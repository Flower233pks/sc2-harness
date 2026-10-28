"""One authoritative experiment loop with bounded policies and durable evidence."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from sc2_harness.backends.sandbox import SandboxBackend
from sc2_harness.evaluation import evaluate_mission, outcome, quantile
from sc2_harness.guidance import reference_plan
from sc2_harness.missions import get_mission, get_role_task, mission_catalog
from sc2_harness.models import ROLES, TERMINAL, RunConfig, initial_agents
from sc2_harness.planning import action_context
from sc2_harness.policies import LLMPolicy, ScriptedPolicy
from sc2_harness.settings import Settings
from sc2_harness.storage import Store, utcnow
from sc2_harness.telemetry import receipt_feedback, record_receipt, source_fingerprint, update_metrics
from sc2_harness.tools import arbitrate, catalog

_EXPORT_LOCK_GUARD = threading.Lock()
_EXPORT_LOCKS: dict[str, threading.Lock] = {}


class RunConflict(ValueError):
    pass


class RunManager:
    def __init__(self, settings: Settings, backend_factory: Callable | None = None,
                 policy_factory: Callable | None = None):
        self.settings, self.store = settings, Store(settings.data_dir)
        self.backend_factory, self.policy_factory = backend_factory, policy_factory
        self.store.interrupt_stale()
        self.tasks: dict[str, asyncio.Task] = {}
        self.subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.resume_events: dict[str, asyncio.Event] = {}
        self.stopping: set[str] = set()
        self.started: set[str] = set()
        self.finalizing: set[str] = set()
        self.lock = asyncio.Lock()

    def capabilities(self) -> dict:
        try:
            from sc2_harness.backends.native import NativeBackend
            native = NativeBackend.probe()
        except (ImportError, OSError, RuntimeError) as exc:
            native = {"id": "sc2", "available": False, "reason": self.settings.redact(exc)}
        native.setdefault("id", "sc2")
        native.setdefault("label", "真实 StarCraft II 引擎")
        return {"backends": [native, SandboxBackend.probe()],
                "roles": [{"id": a["id"], "name": a["name"]} for a in initial_agents()],
                "tools": catalog(), "player_mode": "one native player / function-level cooperation",
                "scenarios": mission_catalog()}

    def get(self, run_id: str) -> dict:
        return self.store.get(run_id)

    def _persist(self, run: dict) -> None:
        run["updated_at"] = utcnow()
        self.store.put(run)

    def _broadcast(self, run_id: str, value: dict) -> None:
        for queue in list(self.subscribers[run_id]):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(copy.deepcopy(value))

    def _event(self, run: dict, typ: str, message: str, actor_id: str = "system", data: dict | None = None):
        # Config/model credentials are never copied into event payloads.
        payload = {"run_id": run["id"], "time": utcnow(), "game_loop": run.get("observation", {}).get("game_loop"),
                   "round": run["round"], "type": typ, "actor_id": actor_id,
                   "message": self.settings.redact(message), "data": self.settings.clean(data or {})}
        event = self.store.append("events", run["id"], payload)
        self._broadcast(run["id"], {"type": "event", "data": event})
        return event

    def _state(self, run: dict):
        resume = self.resume_events.get(run["id"])
        if resume is not None and not resume.is_set() and run["status"] not in TERMINAL | {"stopping"}:
            run["paused"], run["status"] = True, "paused"
        update_metrics(run)
        self._persist(run)
        self._broadcast(run["id"], {"type": "state", "data": run})

    async def create(self, config: RunConfig) -> dict:
        async with self.lock:
            active = [task for task in self.tasks.values() if not task.done()]
            if len(active) >= self.settings.max_active_runs:
                raise RunConflict("已有实验运行。请先暂停检查或结束当前实验。")
            if config.policy == "llm" and not self.policy_factory and not self.settings.public_model()["configured"]:
                raise RunConflict("尚未配置模型服务。请配置公司/本地模型地址和模型名；不会自动调用外部付费 API。")
            if config.backend == "sc2" and not self.backend_factory:
                native = self.capabilities()["backends"][0]
                if not native.get("available", False):
                    raise RunConflict("SC2 环境不可用：" + str(native.get("reason", native)))
            now, run_id = utcnow(), uuid4().hex
            label = self.settings.model_name if config.policy == "llm" and self.settings.model_name else config.policy
            run = {"id": run_id, "name": config.name or f"{config.map_name} / {label}",
                   "status": "starting", "created_at": now, "updated_at": now, "round": 0,
                   "backend": config.backend, "policy": config.policy, "scenario": config.scenario,
                   "model_name": self.settings.model_name if config.policy == "llm" else None,
                   "source_fingerprint": source_fingerprint(), "evaluation_version": "staged-v4-phase-handoff",
                   "map_name": config.map_name, "result": None, "error": None, "paused": False,
                   "config": config.model_dump(), "observation": {}, "agents": initial_agents(),
                   "milestones": [], "evaluation": {}, "termination_reason": None,
                   "metrics": {"model_calls": 0, "model_errors": 0,
                       "input_tokens": 0, "output_tokens": 0, "commands_proposed": 0,
                       "commands_accepted": 0, "commands_rejected": 0, "commands_completed": 0,
                       "commands_execution_failed": 0, "commands_execution_partial": 0,
                       "latency_p50_ms": None, "latency_p95_ms": None,
                       "game_seconds": 0, "wall_seconds": 0, "engine_result": None},
                   "frame_path": None, "artifacts": []}
            self.store.run_dir(run_id)
            self._persist(run)
            self._event(run, "run.created", "创建实验", data={"backend": config.backend, "policy": config.policy,
                        "config": config.model_dump(), "native_player_count": 1,
                        "model_name": run["model_name"], "source_fingerprint": run["source_fingerprint"],
                        "evaluation_version": run["evaluation_version"]})
            resume = asyncio.Event()
            resume.set()
            self.resume_events[run_id] = resume
            self.tasks[run_id] = asyncio.create_task(self._drive(run_id), name=f"sc2-run-{run_id}")
            return run

    def _backend(self, config: RunConfig):
        if self.backend_factory:
            return self.backend_factory(config)
        if config.backend == "sandbox":
            return SandboxBackend()
        from sc2_harness.backends.native import NativeBackend
        return NativeBackend()

    def _policy(self, config: RunConfig):
        if self.policy_factory:
            return self.policy_factory(config)
        if config.policy == "scripted":
            return ScriptedPolicy()
        profile = (
            {"enable_thinking": True, "reasoning_budget_tokens": config.reasoning_budget_tokens,
             "max_output_tokens": 1536, "history_turns": 1}
            if config.reasoning_budget_tokens > 0 else {}
        )
        return LLMPolicy(self.settings.model_base_url, self.settings.model_name,
                         self.settings.model_api_key, timeout=config.decision_timeout,
                         context_window_tokens=self.settings.model_context_tokens, **profile)

    async def control(self, run_id: str, action: str) -> dict:
        run = self.get(run_id)
        task = self.tasks.get(run_id)
        if run["status"] in TERMINAL or task is None or task.done():
            raise RunConflict("实验已经结束；请新建实验，不会重放历史动作。")
        if run_id in self.stopping:
            if action == "stop":
                return run  # Idempotent: a second cancellation could interrupt engine cleanup.
            raise RunConflict("实验正在停止，不能继续或暂停。")
        if run_id in self.finalizing:
            raise RunConflict("实验正在保存证据并关闭引擎。")
        event = self.resume_events[run_id]
        if action == "pause":
            run["paused"], run["status"] = True, "paused"
            event.clear()
            self._event(run, "run.pause_requested", "请求暂停；当前引擎操作收尾后保持暂停")
        elif action == "resume":
            run["paused"], run["status"] = False, "running"
            event.set()
            self._event(run, "run.resumed", "继续实验")
        elif action == "stop":
            self.stopping.add(run_id)
            run["status"] = "stopping"
            self._event(run, "run.stop_requested", "请求结束实验并关闭本次游戏进程")
            # A task cancelled before its first coroutine instruction cannot run its finally block.
            if run_id in self.started:
                task.cancel()
            event.set()
        else:
            raise ValueError("Unknown control action")
        self._state(run)
        return run

    async def _checkpoint(self, run: dict):
        await self.resume_events[run["id"]].wait()
        if run["id"] in self.stopping:
            raise asyncio.CancelledError()
        latest = self.get(run["id"])
        run["paused"] = latest["paused"]

    def _observe(self, run: dict, output: dict):
        observation = output["observation"]
        if observation.get("engine") != run["backend"]:
            raise RuntimeError("Adapter engine identity does not match the selected backend")
        run["observation"] = observation
        prior_stages = {s["id"] for s in run.get("evaluation", {}).get("stages", []) if s.get("complete")}
        run["evaluation"] = evaluate_mission(run["scenario"], observation, run.get("evaluation") or None)
        run["milestones"] = run["evaluation"]["milestones"]
        run["metrics"]["game_seconds"] = observation.get("game_time", 0)
        run["metrics"]["engine_result"] = observation.get("result")
        profile = run["config"].get("guidance_profile", "none")
        previous_plan = run.get("reference_plan")
        plan = reference_plan(profile, observation, previous_plan)
        if plan is not None:
            run["reference_plan"] = plan
            prior_reference_phase = previous_plan.get("phase") if isinstance(previous_plan, dict) else None
            prior_reference_revision = previous_plan.get("revision") if isinstance(previous_plan, dict) else None
            if plan["phase"] != prior_reference_phase or plan["revision"] != prior_reference_revision:
                self._event(run, "coordination.reference_phase_changed", "更新给定参考策略的建议阶段；没有代替模型下达动作",
                            data={"profile": profile, "version": plan["version"], "revision": plan["revision"],
                                  "from_revision": prior_reference_revision, "advisory": True,
                                  "from_phase": prior_reference_phase, "to_phase": plan["phase"],
                                  "observed_at_game_time": plan["observed_at_game_time"],
                                  "soft_targets": plan["soft_targets"], "not_an_agent_action": True})
        frame = output.get("frame_path")
        if frame:
            path = Path(frame)
            root = self.store.run_dir(run["id"])
            path = path if path.is_absolute() else root / path
            if path.resolve().is_relative_to(root.resolve()) and path.is_file():
                run["frame_path"] = str(path.relative_to(root))
            else:
                raise RuntimeError("Frame artifact is outside the run directory or missing")
        self.store.append("snapshots", run["id"], {"round": run["round"], "time": utcnow(),
                                                   "observation": observation,
                                                   **({"reference_plan": copy.deepcopy(plan)} if plan is not None else {})})
        for item in run["milestones"]:
            if item["achieved"] and not item.get("reported"):
                self._event(run, "mission.milestone", item["label"], data=item.copy())
                item["reported"] = True
        for stage in run["evaluation"].get("stages", []):
            if stage.get("complete") and stage["id"] not in prior_stages:
                self._event(run, "mission.stage_completed", stage.get("label", stage["id"]), data=stage)

    async def _drive(self, run_id: str):
        self.started.add(run_id)
        run = self.get(run_id)
        config = RunConfig(**run["config"])
        backend, policy = None, None
        started, latencies, messages = time.monotonic(), [], []
        round_latencies: list[float] = []
        prior_phase: str | None = None
        last_receipts: list[dict] = []
        inflight: list[dict] = []
        try:
            await self._checkpoint(run)
            backend = self._backend(config)
            policy = self._policy(config)
            output = await asyncio.wait_for(backend.start(config.model_dump(), self.store.run_dir(run_id)), 180)
            self._observe(run, output)
            self._event(run, "engine.started", "引擎就绪；决策期间采用暂停步进",
                        data={"engine": config.backend, "rendering": run["observation"].get("rendering")})
            error_streak = 0
            for turn in range(1, config.max_rounds + 1):
                await self._checkpoint(run)
                run["round"], run["status"] = turn, "deciding"
                for agent in run["agents"]:
                    agent["status"] = "deciding"
                self._state(run)
                obs = copy.deepcopy(run["observation"])
                obs["guidance_enabled"] = config.execution_guidance
                obs["task"] = {"scenario": config.scenario,
                               "objective": get_mission(config.scenario)["objective"],
                               "milestones": copy.deepcopy(run["milestones"])}
                current_phase = (run["evaluation"].get("current_stage") or {}).get("id")
                if config.execution_guidance and current_phase != prior_phase:
                    # Old messages remain in durable Trace, but are not current instructions.
                    discarded = len(messages)
                    messages = [message for message in messages if message.get("phase") == current_phase]
                    handoff = {
                        "from_phase": prior_phase, "to_phase": current_phase,
                        "observed_game_time": obs.get("game_time"),
                        "completed_stages": [stage["id"] for stage in run["evaluation"].get("stages", [])
                                             if stage.get("complete")],
                        "mission_complete": bool(run["evaluation"].get("complete")),
                    }
                    obs["task"]["phase_handoff"] = handoff
                    self._event(run, "coordination.phase_handoff", "交接当前阶段，归档旧阶段消息",
                                data={**handoff, "archived_messages": discarded - len(messages)})
                prior_phase = current_phase
                role_objectives = {
                    "economy": "Maintain resource income, reach at least 14 workers and finish one supply depot.",
                    "production": "Finish one barracks and train four ready marines. A supply depot is an economy task; "
                                  "an engineering bay is not a supply depot and is not needed for this mission.",
                    "tactics": "Send an owned unit to physically scout the enemy starting region. "
                               "Only observation.scouted=true confirms completion; an accepted move does not.",
                } if config.scenario == "build_and_scout" else {
                    "economy": "Provide workers, resource income, supply and expansion for the team to win.",
                    "production": "Build production, army and appropriate upgrades for the team to win.",
                    "tactics": "Scout, coordinate combat and preserve the army to win the match.",
                }
                # Each role receives only game-visible units, its task/state, and delivered messages.
                requests, requested_roles = [], []
                completed_roles: dict[str, dict] = {}
                for role in ROLES:
                    role_obs = copy.deepcopy(obs)
                    role_obs["task"].update(role=role, role_objective=role_objectives[role])
                    if config.execution_guidance:
                        role_obs["task"].update(get_role_task(role, run["evaluation"], role_obs,
                                                            reference_plan=run.get("reference_plan")))
                        role_obs["action_context"] = action_context(role, role_obs)
                    role_obs["last_receipts"] = receipt_feedback(last_receipts, role)
                    if config.execution_guidance and role_obs["task"].get("role_done"):
                        # The coordinator owns task lifecycle, never invents gameplay actions.
                        # This is explicitly recorded as waiting, not a fake model response.
                        completed_roles[role] = role_obs["task"]
                        continue
                    requested_roles.append(role)
                    requests.append(policy.decide(role, role_obs,
                        [m for m in messages if m["to"] in ("all", role)] if config.communication else []))
                run["metrics"]["role_decisions_scheduled"] = (
                    run["metrics"].get("role_decisions_scheduled", 0) + len(requested_roles))
                self._event(run, "coordination.decision_round", "本轮活跃角色与等待角色",
                            data={"active_roles": requested_roles, "waiting_roles": list(completed_roles),
                                  "phase": (run["evaluation"].get("current_stage") or {}).get("id")})
                round_started = time.perf_counter()
                try:
                    outputs = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True),
                                                     config.decision_timeout + 2)
                finally:
                    round_latencies.append((time.perf_counter() - round_started) * 1000)
                    run["metrics"]["round_decision_latency_p50_ms"] = quantile(round_latencies, .5)
                    run["metrics"]["round_decision_latency_p95_ms"] = quantile(round_latencies, .95)
                role_outputs = dict(zip(requested_roles, outputs))
                decisions, next_messages, errors = {}, [], 0
                for agent in run["agents"]:
                    role = agent["id"]
                    if role in completed_roles:
                        task = completed_roles[role]
                        agent.update(status="waiting", goal="当前分工目标已完成，等待下一阶段",
                                     last_reason="根据已观察到的任务进度暂停决策；没有代替模型选择游戏动作。",
                                     last_actions=[])
                        if agent.get("waiting_phase") != task["role_phase"]:
                            self._event(run, "coordination.role_wait", "当前分工完成，等待新阶段", role,
                                {"phase": task["role_phase"], "role_milestones": task.get("role_milestones"),
                                 "request_count": 0, "actions": []})
                        agent["waiting_phase"] = task["role_phase"]
                        run["metrics"]["coordinator_wait_role_rounds"] = (
                            run["metrics"].get("coordinator_wait_role_rounds", 0) + 1)
                        continue
                    agent.pop("waiting_phase", None)
                    decision = role_outputs[role]
                    role, stats = agent["id"], agent["stats"]
                    failed = isinstance(decision, BaseException)
                    metadata = decision if not failed else {
                        key: getattr(decision, key, None)
                        for key in ("request_count", "usage", "usage_reported", "prompt_messages", "context_recovery",
                                    "inference_settings", "provider_diagnostics", "attempt_diagnostics")}
                    calls = int(metadata.get("request_count") or 0)
                    stats["calls"] += calls
                    usage = metadata.get("usage") or {}
                    for key in ("input_tokens", "output_tokens"):
                        value = usage.get(key)
                        if calls and value is None:
                            stats[key] = None
                        elif stats[key] is not None and value is not None:
                            stats[key] += int(value)
                    if isinstance(decision, BaseException):
                        errors += 1
                        stats["errors"] += 1
                        agent["status"] = "error"
                        self._event(run, "agent.error", self.settings.redact(decision), role,
                                    {"code": getattr(decision, "code", "decision_error"),
                                     "request_count": calls, "usage": usage,
                                     "usage_reported": bool(metadata.get("usage_reported")),
                                     "prompt_messages": metadata.get("prompt_messages"),
                                     "context_recovery": metadata.get("context_recovery"),
                                     "inference_settings": metadata.get("inference_settings"),
                                     "provider_diagnostics": metadata.get("provider_diagnostics"),
                                     "attempt_diagnostics": metadata.get("attempt_diagnostics")})
                        continue
                    # Only an explicitly public, validated decision enters durable trace; no hidden reasoning.
                    public = {key: decision.get(key) for key in ("goal", "reason", "actions", "messages")}
                    public = self.settings.clean(public)
                    decisions[role] = public
                    agent.update(status="ready", goal=public["goal"], last_reason=public["reason"],
                                 last_actions=public["actions"])
                    agent["messages"] = [m for m in messages if m["to"] in ("all", role)][-10:]
                    latency = float(decision.get("latency_ms", 0))
                    stats["last_latency_seconds"] = latency / 1000 if config.policy == "llm" else None
                    if config.policy == "llm":
                        latencies.append(latency)
                    self._event(run, "agent.decision", public.get("reason", ""), role,
                                {**public, "usage": usage, "latency_ms": latency,
                                 "request_count": calls, "usage_reported": bool(metadata.get("usage_reported")),
                                 "prompt_messages": metadata.get("prompt_messages"),
                                 "model": decision.get("model", config.policy), "policy": config.policy,
                                 "guidance_enabled": decision.get("guidance_enabled", config.execution_guidance),
                                 "available_tools": decision.get("available_tools"),
                                 "context_reset": decision.get("context_reset"),
                                  "context_recovery": decision.get("context_recovery"),
                                  "inference_settings": decision.get("inference_settings"),
                                  "provider_diagnostics": decision.get("provider_diagnostics"),
                                  "attempt_diagnostics": decision.get("attempt_diagnostics")})
                    run["metrics"]["empty_action_decisions"] = (
                        run["metrics"].get("empty_action_decisions", 0) + int(not public.get("actions"))
                    )
                    if config.communication:
                        for message in (public.get("messages") or [])[:2]:
                            item = {"from": role, "to": message["to"], "text": message["text"], "round": turn}
                            if config.execution_guidance:
                                item.update(phase=current_phase, game_time=obs.get("game_time"))
                            next_messages.append(item)
                            self._event(run, "agent.message", item["text"], role, item)
                error_streak = error_streak + 1 if requested_roles and errors == len(requested_roles) else 0
                run["metrics"]["model_errors"] += errors
                run["metrics"]["model_calls"] = sum(a["stats"]["calls"] for a in run["agents"])
                for key in ("input_tokens", "output_tokens"):
                    values = [a["stats"][key] for a in run["agents"]]
                    run["metrics"][key] = None if any(v is None for v in values) else sum(values)
                run["metrics"]["latency_p50_ms"] = quantile(latencies, .5)
                run["metrics"]["latency_p95_ms"] = quantile(latencies, .95)
                if error_streak >= 3:
                    raise RuntimeError("当前活跃 Agent 连续三轮全部决策失败，实验已停止；没有切换为脚本策略。")
                self._state(run)  # A paused run must still expose the inference that just finished.
                await self._checkpoint(run)  # Record returned inference even when pausing before game orders.
                offset = turn % len(ROLES) if config.execution_guidance else 0
                priority = ROLES[offset:] + ROLES[:offset]
                arbitration_obs = run["observation"] if config.execution_guidance else {
                    k: v for k, v in run["observation"].items() if k != "action_state"}
                self._event(run, "coordination.budget_order", "本轮共享资源处理顺序",
                            data={"priority": list(priority), "guidance_enabled": config.execution_guidance})
                commands, rejected = arbitrate(decisions, arbitration_obs, priority=priority)
                run["metrics"]["commands_proposed"] += len(commands) + len(rejected)
                for command in commands:
                    self._event(run, "command.proposed", command["tool"], command["actor_id"], command)
                for receipt in rejected:
                    run["metrics"]["commands_rejected"] += 1
                    record_receipt(run["metrics"], receipt)
                    self._event(run, "command.rejected", receipt["reason"], receipt["actor_id"], receipt)
                run["status"] = "running"
                self._state(run)
                inflight = commands
                output = await asyncio.wait_for(backend.step(commands, config.advance_loops), 90)
                inflight = []
                last_receipts = rejected + output.get("receipts", [])
                for receipt in output.get("receipts", []):
                    record_receipt(run["metrics"], receipt)
                    status = receipt.get("status", "rejected")
                    metric = {"accepted": "commands_accepted", "rejected": "commands_rejected",
                              "completed": "commands_completed"}.get(status)
                    if metric:
                        run["metrics"][metric] += 1
                    self._event(run, f"command.{status}", receipt.get("reason", receipt.get("tool", "")),
                                receipt.get("actor_id", "system"), receipt)
                    execution = (receipt.get("details") or {}).get("execution_status")
                    if status == "accepted" and execution in {"failed", "partial"}:
                        self._event(run, f"command.execution_{execution}",
                                    "原生引擎随后报告执行失败" if execution == "failed" else "原生引擎随后报告部分执行失败",
                                    receipt.get("actor_id", "system"), receipt)
                self._observe(run, output)
                messages = (messages + next_messages)[-30:] if config.communication else []
                for agent in run["agents"]:
                    if agent["status"] not in {"error", "waiting"}:
                        agent["status"] = "idle"
                run["metrics"]["wall_seconds"] = round(time.monotonic() - started, 2)
                self._state(run)
                result = outcome(config.scenario, run["observation"], run["milestones"])
                if result or output.get("terminal"):
                    run["result"] = result or "incomplete"
                    run["termination_reason"] = "mission_success" if result == "success" else "engine_terminal"
                    break
                if run["observation"].get("game_time", 0) >= config.max_game_seconds:
                    run["termination_reason"] = "game_time_budget"
                    break
                # Yield even for a fully local test adapter, so controls/streams remain responsive.
                await asyncio.sleep(.15 if config.backend == "sandbox" else 0)
            run.update(status="completed", result=run["result"] or "incomplete",
                       termination_reason=run["termination_reason"] or "round_budget")
        except asyncio.CancelledError:
            run.update(status="stopped", result="stopped", termination_reason="user_stop")
        except Exception as exc:  # noqa: BLE001 — isolate a failed external engine/model run.
            run.update(status="failed", result="error", error=self.settings.redact(exc), termination_reason="runtime_error")
            self._event(run, "run.error", run["error"], data={"type": type(exc).__name__})
        finally:
            self.finalizing.add(run_id)
            for command in inflight:
                self._event(run, "command.outcome_unknown", "操作中断，执行结果不明；不会自动重放", command["actor_id"], command)
            if backend is not None:
                try:
                    closed = await asyncio.wait_for(backend.close(), 15)
                    self._event(run, "engine.closed", "本次引擎已关闭", data=closed or {})
                except Exception as exc:  # noqa: BLE001 — cleanup failures must remain visible in evidence.
                    run["cleanup_error"] = self.settings.redact(exc)
                    self._event(run, "engine.cleanup_failed", run["cleanup_error"],
                                data={"type": type(exc).__name__})
            if policy and hasattr(policy, "close"):
                with suppress(Exception):
                    await asyncio.wait_for(policy.close(), 5)
            # A policy can expose a live ledger including HTTP requests cancelled during inference.
            accounting = getattr(policy, "accounting", None)
            if isinstance(accounting, dict):
                for agent in run["agents"]:
                    recorded = accounting.get(agent["id"], {})
                    for key in ("calls", "input_tokens", "output_tokens"):
                        if key in recorded:
                            agent["stats"][key] = recorded[key]
                    # An inactive role made no HTTP request, so its usage is known zero.
                    # Unknown usage only applies to requests that were actually attempted.
                    if agent["stats"]["calls"] == 0:
                        agent["stats"]["input_tokens"] = agent["stats"]["output_tokens"] = 0
                run["metrics"]["model_calls"] = sum(a["stats"]["calls"] for a in run["agents"])
                for key in ("input_tokens", "output_tokens"):
                    values = [a["stats"][key] for a in run["agents"]]
                    run["metrics"][key] = None if any(v is None for v in values) else sum(values)
            run["paused"] = False
            run["metrics"]["wall_seconds"] = round(time.monotonic() - started, 2)
            if run.get("observation"):
                run["evaluation"] = evaluate_mission(config.scenario, run["observation"], run.get("evaluation"),
                    terminated=True, termination_reason=run.get("termination_reason"))
                run["milestones"] = run["evaluation"]["milestones"]
            for agent in run["agents"]:
                agent["status"] = "finished"
            self._event(run, "run.finished", "实验结束", data={"result": run["result"], "error": run["error"]})
            self._state(run)
            self.resume_events.pop(run_id, None)
            self.stopping.discard(run_id)
            self.started.discard(run_id)
            try:
                self.export(run_id)
            except OSError as exc:
                run["export_error"] = self.settings.redact(exc)
                self._event(run, "artifact.export_failed", run["export_error"])
                self._state(run)
            self.finalizing.discard(run_id)

    def export(self, run_id: str):
        directory = self.store.run_dir(run_id)
        # HTTP artifact requests run in threads, while finalization can export on the loop.
        # Serialize this run across managers and capture all evidence in one read snapshot.
        with _EXPORT_LOCK_GUARD:
            lock = _EXPORT_LOCKS.setdefault(str(directory.resolve()), threading.Lock())
        with lock:
            with self.store.connect() as db:
                db.execute("BEGIN")
                row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                run = json.loads(row[0])
                evidence = {}
                for table in ("events", "snapshots"):
                    rows = db.execute(
                        f"SELECT seq,payload FROM {table} WHERE run_id=? ORDER BY seq", (run_id,)
                    ).fetchall()
                    evidence[table] = [{"seq": row[0], **json.loads(row[1])} for row in rows]
            pending: list[tuple[Path, Path]] = []
            try:
                documents = {"run.json": run, "metrics.json": run["metrics"], "config.json": run["config"]}
                for name in (*documents, "events.jsonl", "snapshots.jsonl"):
                    target = directory / name
                    temporary = directory / f".{name}.{uuid4().hex}.tmp"
                    pending.append((temporary, target))
                    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                        if name in documents:
                            json.dump(documents[name], stream, ensure_ascii=False, allow_nan=False, indent=2)
                        else:
                            for item in evidence[name.removesuffix(".jsonl")]:
                                stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                # All writes succeeded before publishing; downloads see complete old/new files.
                for temporary, target in pending:
                    os.replace(temporary, target)
            finally:
                for temporary, _ in pending:
                    temporary.unlink(missing_ok=True)

    async def shutdown(self):
        for run_id, task in list(self.tasks.items()):
            if not task.done():
                already_stopping = run_id in self.stopping
                self.stopping.add(run_id)
                if run_id in self.started and run_id not in self.finalizing and not already_stopping:
                    task.cancel()
        if self.tasks:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*self.tasks.values(), return_exceptions=True)), 30)

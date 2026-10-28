import asyncio
import json
from pathlib import Path

import pytest

from sc2_harness.backends.sandbox import SandboxBackend
from sc2_harness.models import RunConfig
from sc2_harness.policies import LLMPolicy, PolicyError, ScriptedPolicy
from sc2_harness.runtime import RunConflict, RunManager
from sc2_harness.settings import Settings
from sc2_harness.storage import Store


async def finish(manager, run_id):
    await asyncio.wait_for(manager.tasks[run_id], 10)
    return manager.get(run_id)


async def test_sandbox_run_has_real_receipts_and_explicit_evidence_labels(tmp_path):
    manager = RunManager(Settings(tmp_path))
    run = await manager.create(RunConfig(backend="sandbox", max_rounds=20))
    result = await finish(manager, run["id"])
    assert result["status"] == "completed"
    assert result["observation"]["engine"] == "sandbox"
    assert result["observation"]["rendering"] == "tactical"
    assert result["metrics"]["commands_accepted"] > 0
    assert result["metrics"]["model_calls"] == 0
    events = list(manager.store.all_evidence("events", run["id"]))
    assert any(e["type"] == "command.accepted" for e in events)
    # A latched milestone is reported once, not once per observation.
    milestones = [e["data"]["id"] for e in events if e["type"] == "mission.milestone"]
    assert len(milestones) == len(set(milestones))
    assert json.loads((manager.store.run_dir(run["id"]) / "metrics.json").read_text())["model_calls"] == 0


async def test_shared_manager_prevents_two_authoritative_games(tmp_path):
    manager = RunManager(Settings(tmp_path))
    run = await manager.create(RunConfig(backend="sandbox"))
    with pytest.raises(RunConflict):
        await manager.create(RunConfig(backend="sandbox"))
    await manager.control(run["id"], "stop")
    result = await finish(manager, run["id"])
    assert result["result"] == "stopped"


async def test_native_late_error_has_its_own_event_and_keeps_initial_ack(tmp_path):
    class LateError(SandboxBackend):
        async def step(self, commands, advance_loops):
            output = await super().step(commands, advance_loops)
            accepted = next(receipt for receipt in output["receipts"] if receipt["status"] == "accepted")
            accepted["details"].update(execution_status="failed", engine_results=[1], native_errors=[{
                "unit_tag": 7, "ability_id": 321, "result": 208, "result_name": "CouldntReachTarget"}])
            return output

    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: LateError())
    run = await manager.create(RunConfig(backend="sandbox", max_rounds=1))
    result = await finish(manager, run["id"])
    events = list(manager.store.all_evidence("events", run["id"]))
    failures = [event for event in events if event["type"] == "command.execution_failed"]
    assert len(failures) == 1 and failures[0]["data"]["status"] == "accepted"
    assert result["metrics"]["commands_execution_failed"] == 1
    assert result["metrics"]["commands_accepted"] > 0
    assert result["metrics"]["commands_completed"] == 0


async def test_guided_task_goals_and_round_metrics_do_not_choose_actions(tmp_path):
    observations = {}

    class ObserveOnly:
        async def decide(self, role, observation, messages):
            observations[role] = observation
            return {"goal": "Observe", "reason": "Wait deliberately", "actions": [], "messages": [],
                    "request_count": 0, "latency_ms": 0, "usage": {"input_tokens": 0, "output_tokens": 0}}

    manager = RunManager(Settings(tmp_path), policy_factory=lambda config: ObserveOnly())
    run = await manager.create(RunConfig(backend="sandbox", max_rounds=1))
    result = await finish(manager, run["id"])
    assert set(observations) == {"economy", "production", "tactics"}
    assert observations["production"]["task"]["goal_gaps"]
    assert observations["economy"]["task"]["role_phase"] == "build_and_scout"
    assert observations["economy"]["action_context"]["availability_known"] is False
    assert result["metrics"]["commands_proposed"] == 0
    assert result["metrics"]["command_acceptance_rate"] is None
    assert result["metrics"]["empty_action_decisions"] == 3
    assert result["metrics"]["round_decision_latency_p50_ms"] is not None
    assert result["termination_reason"] == "round_budget"
    assert result["evaluation"]["censored"] is True
    assert len(result["source_fingerprint"]) == 64
    events = list(manager.store.all_evidence("events", run["id"]))
    ordering = next(event for event in events if event["type"] == "coordination.budget_order")
    assert ordering["data"]["priority"] == ["production", "tactics", "economy"]


async def test_guidance_off_retains_explicit_comparison_without_fake_feasibility(tmp_path):
    observations = []

    class ObserveOnly:
        async def decide(self, role, observation, messages):
            observations.append(observation)
            return {"goal": "Observe", "reason": "Wait deliberately", "actions": [], "messages": []}

    manager = RunManager(Settings(tmp_path), policy_factory=lambda config: ObserveOnly())
    run = await manager.create(RunConfig(backend="sandbox", max_rounds=1, execution_guidance=False))
    result = await finish(manager, run["id"])
    assert all(obs["guidance_enabled"] is False and "action_context" not in obs for obs in observations)
    assert all("goal_gaps" not in obs["task"] for obs in observations)
    events = list(manager.store.all_evidence("events", run["id"]))
    ordering = next(event for event in events if event["type"] == "coordination.budget_order")
    assert ordering["data"]["priority"] == ["economy", "production", "tactics"]
    assert result["metrics"]["model_calls"] == 0


async def test_completed_role_waits_without_fake_inference_and_reactivates_next_stage(tmp_path):
    called = []

    class OpeningProgress(SandboxBackend):
        def _observation(self):
            observation = super()._observation()
            observation.update(workers=14, structures={"COMMANDCENTER": 1, "SUPPLYDEPOT": 1,
                               **({"BARRACKS": 1} if self.loop else {})},
                               score={"collected_vespene": 0})
            return observation

    class ObserveOnly:
        def __init__(self):
            self.calls = {"economy": 0, "production": 0, "tactics": 0}

        @property
        def accounting(self):
            return {role: {"calls": count, "input_tokens": count * 10 if count else None,
                           "output_tokens": count * 2 if count else None}
                    for role, count in self.calls.items()}

        async def decide(self, role, observation, messages):
            called.append((role, observation["task"]["role_phase"]))
            self.calls[role] += 1
            return {"goal": "Observe", "reason": "Wait deliberately", "actions": [], "messages": [],
                    "request_count": 1, "usage": {"input_tokens": 10, "output_tokens": 2}}

    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: OpeningProgress(),
                         policy_factory=lambda config: ObserveOnly())
    run = await manager.create(RunConfig(backend="sandbox", policy="llm", scenario="army_readiness", max_rounds=2))
    result = await finish(manager, run["id"])
    assert called == [("production", "opening"), ("economy", "infantry_infrastructure"),
                      ("production", "infantry_infrastructure")]
    assert result["metrics"]["model_calls"] == 3
    assert result["metrics"]["input_tokens"] == 30
    assert result["metrics"]["output_tokens"] == 6
    assert result["metrics"]["coordinator_wait_role_rounds"] == 3
    events = list(manager.store.all_evidence("events", run["id"]))
    assert len([event for event in events if event["type"] == "agent.decision"]) == 3
    assert all(not event["data"]["actions"] and event["data"]["request_count"] == 0
               for event in events if event["type"] == "coordination.role_wait")
    assert any(event["type"] == "mission.stage_completed" for event in events)


@pytest.mark.parametrize("guided", [True, False])
async def test_phase_handoff_archives_expired_messages_but_preserves_trace_and_same_phase(tmp_path, guided):
    calls = []

    class OpeningProgress(SandboxBackend):
        def _observation(self):
            observation = super()._observation()
            observation.update(workers=14, structures={"COMMANDCENTER": 1, "SUPPLYDEPOT": 1,
                               **({"BARRACKS": 1} if self.loop else {})}, score={"collected_vespene": 0})
            return observation

    class Messenger:
        async def decide(self, role, observation, messages):
            calls.append((role, observation["game_loop"], observation["task"], list(messages)))
            return {"goal": "Observe", "reason": "No gameplay action selected", "actions": [],
                    "messages": [{"to": "all", "text": f"observation at {observation['game_loop']}"}]}

    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: OpeningProgress(),
                         policy_factory=lambda config: Messenger())
    run = await manager.create(RunConfig(backend="sandbox", scenario="army_readiness", max_rounds=3,
                                        execution_guidance=guided))
    result = await finish(manager, run["id"])
    second = [call for call in calls if call[1] == 112]
    third = [call for call in calls if call[1] == 224]
    events = list(manager.store.all_evidence("events", run["id"]))
    if guided:
        assert all(not call[3] for call in second)
        assert all(call[2]["phase_handoff"]["from_phase"] == "opening" for call in second)
        assert all(call[2]["phase_handoff"]["mission_complete"] is False for call in second)
        assert all(call[3] and all(message["phase"] == "infantry_infrastructure" and message["round"] == 2
                                  for message in call[3]) for call in third)
        assert any(event["type"] == "agent.message" and event["data"].get("phase") == "opening"
                   for event in events)
        assert any(event["type"] == "coordination.phase_handoff" and event["data"]["archived_messages"] == 1
                   for event in events)
    else:
        assert all(call[3] and call[3][0]["round"] == 1 for call in second)
        assert not any(event["type"] == "coordination.phase_handoff" for event in events)
    assert result["metrics"]["commands_proposed"] == 0


async def test_pause_during_step_is_not_overwritten_and_stop_cleans_backend(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowBackend(SandboxBackend):
        async def step(self, commands, advance_loops):
            entered.set()
            await release.wait()
            return await super().step(commands, advance_loops)

    backend = SlowBackend()
    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: backend)
    run = await manager.create(RunConfig(backend="sandbox", max_rounds=30))
    await asyncio.wait_for(entered.wait(), 3)
    await manager.control(run["id"], "pause")
    release.set()
    await asyncio.sleep(.05)
    paused = manager.get(run["id"])
    assert paused["status"] == "paused" and paused["paused"]
    before = paused["round"]
    await asyncio.sleep(.1)
    assert manager.get(run["id"])["round"] == before
    await manager.control(run["id"], "stop")
    await finish(manager, run["id"])
    assert backend.closed


async def test_backend_failure_never_becomes_sandbox_or_success(tmp_path):
    class Broken(SandboxBackend):
        async def start(self, config, run_dir):
            raise RuntimeError("Native worker could not start")

        async def close(self):
            return {"closed": True}

    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: Broken())
    run = await manager.create(RunConfig(backend="sc2"))
    result = await finish(manager, run["id"])
    assert result["backend"] == "sc2"
    assert result["status"] == "failed" and result["result"] == "error"
    assert result["observation"] == {}


@pytest.mark.parametrize("budget", [0, 512])
async def test_unconfigured_model_rejected_before_game_is_created(tmp_path, budget):
    manager = RunManager(Settings(tmp_path))
    with pytest.raises(RunConflict, match="模型"):
        await manager.create(RunConfig(backend="sandbox", policy="llm", reasoning_budget_tokens=budget))
    assert manager.store.list() == []


@pytest.mark.parametrize("budget", [0, 512, 1024])
async def test_runtime_factory_applies_only_the_explicit_reasoning_profile_without_inference(tmp_path, budget):
    manager = RunManager(Settings(tmp_path, "http://localhost/v1", "mock-model"))
    policy = manager._policy(RunConfig(policy="llm", reasoning_budget_tokens=budget, decision_timeout=12))
    try:
        assert isinstance(policy, LLMPolicy)
        assert policy.timeout == 12
        profile = policy.inference_settings
        assert profile["enable_thinking"] is (budget > 0)
        assert profile["reasoning_budget_tokens"] == (budget or None)
        assert profile["max_output_tokens"] == (1536 if budget else 768)
        assert profile["history_turns"] == (1 if budget else 3)
        assert profile["strict_schema"] is True
        assert all(row["calls"] == 0 for row in policy.accounting.values())
    finally:
        await policy.close()
    # A budget is not permission to silently replace the explicitly selected policy.
    assert isinstance(manager._policy(RunConfig(policy="scripted", reasoning_budget_tokens=budget)), ScriptedPolicy)


def test_reasoning_profile_constructor_failure_never_falls_back_to_a_scripted_policy(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        assert kwargs["enable_thinking"] is True
        raise ValueError("Explicit model service is unavailable")

    monkeypatch.setattr("sc2_harness.runtime.LLMPolicy", unavailable)
    manager = RunManager(Settings(tmp_path, "http://localhost/v1", "mock-model"))
    with pytest.raises(ValueError, match="Explicit model service"):
        manager._policy(RunConfig(policy="llm", reasoning_budget_tokens=512))


async def test_public_inference_profile_is_persisted_without_provider_private_fields(tmp_path):
    profile = {"temperature": 0.7, "enable_thinking": True, "reasoning_format": "deepseek",
               "reasoning_budget_tokens": 512, "max_output_tokens": 1536, "history_turns": 1,
               "strict_schema": True}
    diagnostics = {"has_reasoning_content": True, "finish_reason": "stop"}

    class PublicOnly:
        async def decide(self, role, observation, messages):
            return {"goal": "Wait deliberately", "reason": "No command selected in this mocked policy",
                    "actions": [], "messages": [], "request_count": 1,
                    "usage": {"input_tokens": 20, "output_tokens": 30}, "usage_reported": True,
                    "inference_settings": profile, "provider_diagnostics": diagnostics,
                    "reasoning_content": "PRIVATE_PROVIDER_TEXT_NOT_FOR_TRACE"}

    manager = RunManager(Settings(tmp_path), policy_factory=lambda config: PublicOnly())
    run = await manager.create(RunConfig(backend="sandbox", policy="llm", max_rounds=1,
                                       reasoning_budget_tokens=512))
    result = await finish(manager, run["id"])
    events = list(manager.store.all_evidence("events", run["id"]))
    decisions = [event["data"] for event in events if event["type"] == "agent.decision"]
    assert len(decisions) == 3
    assert all(event["inference_settings"] == profile for event in decisions)
    assert all(event["provider_diagnostics"] == diagnostics for event in decisions)
    assert result["config"]["reasoning_budget_tokens"] == 512
    assert "PRIVATE_PROVIDER_TEXT_NOT_FOR_TRACE" not in json.dumps([events, result])


@pytest.mark.parametrize("window", [None, 8192, 16384])
async def test_runtime_forwards_explicit_configured_context_without_inferring_service_capacity(tmp_path, window):
    manager = RunManager(Settings(tmp_path, "http://localhost/v1", "mock", model_context_tokens=window))
    policy = manager._policy(RunConfig(policy="llm", reasoning_budget_tokens=512))
    try:
        assert policy.inference_settings["context_window_tokens"] == window
        assert policy.inference_settings["context_window_source"] == ("configured" if window else "unknown")
        assert policy.accounting["tactics"]["calls"] == 0
    finally:
        await policy.close()


async def test_failed_attempt_diagnostics_and_configured_context_survive_runtime_trace(tmp_path):
    diagnostics = [{"attempt": 1, "http_status": 200, "finish_reason": "length", "prompt_tokens": 8000,
                    "completion_tokens": 192, "has_reasoning_content": True}]

    class Truncated:
        async def decide(self, role, observation, messages):
            raise PolicyError("context_headroom_exhausted", "No public decision was completed", attempts=1,
                              usage={"input_tokens": 8000, "output_tokens": 192}, usage_reported=True,
                              attempt_diagnostics=diagnostics,
                              inference_settings={"context_window_tokens": 8192, "context_window_source": "configured"})

    manager = RunManager(Settings(tmp_path), policy_factory=lambda config: Truncated())
    run = await manager.create(RunConfig(backend="sandbox", policy="llm", max_rounds=1))
    await finish(manager, run["id"])
    errors = [e["data"] for e in manager.store.all_evidence("events", run["id"]) if e["type"] == "agent.error"]
    assert len(errors) == 3 and all(e["attempt_diagnostics"] == diagnostics for e in errors)
    assert all(e["inference_settings"]["context_window_source"] == "configured" for e in errors)


async def test_unknown_command_result_on_cancel_is_never_replayed(tmp_path):
    entered = asyncio.Event()

    class Blocking(SandboxBackend):
        async def step(self, commands, advance_loops):
            entered.set()
            await asyncio.sleep(10)

    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: Blocking())
    run = await manager.create(RunConfig(backend="sandbox"))
    await asyncio.wait_for(entered.wait(), 3)
    await manager.control(run["id"], "stop")
    await finish(manager, run["id"])
    events = list(manager.store.all_evidence("events", run["id"]))
    assert any(e["type"] == "command.outcome_unknown" for e in events)
    assert not any(e["type"] == "command.accepted" for e in events)


def test_restart_preserves_evidence_and_marks_interruption(tmp_path):
    store = Store(tmp_path)
    run_id = "a" * 32
    store.put({"id": run_id, "status": "deciding", "round": 3, "result": None})
    store.append("events", run_id, {"type": "command.proposed", "data": {"command_id": "c1"}})
    manager = RunManager(Settings(tmp_path))
    assert manager.get(run_id)["status"] == "interrupted"
    events = manager.store.page("events", run_id)["events"]
    assert [event["type"] for event in events] == ["command.proposed", "run.interrupted"]
    assert manager.tasks == {}


def test_settings_clean_does_not_truncate_valid_public_decisions(tmp_path):
    settings = Settings(tmp_path, model_api_key="private-secret")
    data = {"reason": "x" * 4000, "messages": [{"text": "private-secret"}], "api_key": "other-key"}
    cleaned = settings.clean(data)
    assert len(cleaned["reason"]) == 4000
    assert cleaned["messages"][0]["text"] == "[REDACTED]"
    assert cleaned["api_key"] == "[REDACTED]"


def test_run_paths_do_not_escape_data_dir(tmp_path):
    store = Store(tmp_path)
    for value in ("../escape", "a" * 31, "../../.env", str(Path.cwd())):
        with pytest.raises(KeyError):
            store.run_dir(value)


async def test_failed_model_attempts_and_unknown_usage_are_not_reported_as_zero(tmp_path):
    class FailedModel:
        async def decide(self, role, observation, messages):
            assert observation["task"]["scenario"] == "build_and_scout"
            raise PolicyError("timeout", "inference timeout", attempts=2)

    manager = RunManager(Settings(tmp_path), policy_factory=lambda config: FailedModel())
    run = await manager.create(RunConfig(backend="sandbox", policy="llm"))
    result = await finish(manager, run["id"])
    assert result["status"] == "failed"
    assert result["metrics"]["model_calls"] == 18  # three roles, two attempts, three failed rounds
    assert result["metrics"]["model_errors"] == 9
    assert result["metrics"]["input_tokens"] is None
    assert result["metrics"]["output_tokens"] is None
    assert result["metrics"]["latency_p50_ms"] is None


async def test_backend_cleanup_failure_is_visible(tmp_path):
    class BrokenCleanup(SandboxBackend):
        async def close(self):
            raise OSError("replay filesystem unavailable")

    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: BrokenCleanup())
    run = await manager.create(RunConfig(backend="sandbox", max_rounds=1))
    result = await finish(manager, run["id"])
    assert "replay filesystem" in result["cleanup_error"]
    events = list(manager.store.all_evidence("events", run["id"]))
    assert any(e["type"] == "engine.cleanup_failed" for e in events)


async def test_repeated_stop_and_shutdown_do_not_cancel_engine_cleanup(tmp_path):
    closing, release = asyncio.Event(), asyncio.Event()

    class ClosingBackend(SandboxBackend):
        async def close(self):
            closing.set()
            await release.wait()
            return await super().close()

    backend = ClosingBackend()
    manager = RunManager(Settings(tmp_path), backend_factory=lambda config: backend)
    run = await manager.create(RunConfig(backend="sandbox"))
    await asyncio.sleep(.05)
    await manager.control(run["id"], "stop")
    await asyncio.wait_for(closing.wait(), 3)
    await manager.control(run["id"], "stop")
    with pytest.raises(RunConflict):
        await manager.control(run["id"], "resume")
    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(.01)
    release.set()
    await shutdown
    assert backend.closed
    assert manager.get(run["id"])["status"] == "stopped"
    assert (manager.store.run_dir(run["id"]) / "run.json").is_file()

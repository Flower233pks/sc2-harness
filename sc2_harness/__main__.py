from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx

from sc2_harness.models import TERMINAL, RunConfig


def _reasoning_budget(value: str) -> int:
    try:
        tokens = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("reasoning budget must be an integer from 0 to 1024") from exc
    if not 0 <= tokens <= 1024:
        raise argparse.ArgumentTypeError("reasoning budget must be an integer from 0 to 1024")
    return tokens


def _record(config: RunConfig, run: dict | None, error: BaseException | None = None) -> dict:
    run = run or {}
    metrics = run.get("metrics", {})
    return {"id": run.get("id"), "config": run.get("config", config.model_dump()),
            "status": run.get("status", "unconfirmed"), "result": run.get("result"),
            "metrics": metrics, "error": run.get("error"), "model_name": run.get("model_name"),
            "evaluation": run.get("evaluation"), "termination_reason": run.get("termination_reason"),
            "source_fingerprint": run.get("source_fingerprint"), "evaluation_version": run.get("evaluation_version"),
            "reference_plan": run.get("reference_plan"),
            "orchestration_error": str(error) or type(error).__name__ if error is not None else None,
            "native_game_evidence": run.get("observation", {}).get("engine") == "sc2",
            "real_model_evidence": metrics.get("model_calls", 0) > 0}


def _write_report(args, records: list[dict], planned_count: int, complete: bool) -> None:
    report = {"backend": args.backend, "policy": args.policy, "sample_count": len(records),
              "reasoning_budget_tokens": getattr(args, "reasoning_budget_tokens", 0),
              "guidance_profile": getattr(args, "guidance_profile", "none"),
              "planned_sample_count": planned_count, "complete": complete,
              "successes": sum(r["result"] == "success" for r in records), "runs": records,
              "outcome_counts": {result: sum(r.get("result") == result for r in records)
                                 for result in ("success", "failure", "incomplete", "error", "stopped")}
                                 | {"unconfirmed": sum(r.get("result") is None for r in records)},
              "observed_success_rate": sum(r["result"] == "success" for r in records) / len(records) if records else None,
              "native_game_evidence": bool(records) and all(r["native_game_evidence"] for r in records),
              "real_model_evidence": bool(records) and all(r["real_model_evidence"] for r in records),
              "note": "Evidence flags indicate observed engine state / attempted model HTTP requests, not task "
                      "success. Partial reports preserve last observed state; orchestration errors do not "
                      "rewrite the server run or imply that it stopped."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="SC2 Harness 实验工作台")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="启动本机网页与 API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18890)
    commands.add_parser("doctor", help="只读检查游戏与模型配置，不启动游戏或调用模型")
    for name in ("run", "evaluate"):
        run = commands.add_parser(name)
        run.add_argument("--url", default="http://127.0.0.1:18890")
        run.add_argument("--backend", choices=["sc2", "sandbox"], default="sc2")
        run.add_argument("--policy", choices=["scripted", "llm"], default="scripted")
        run.add_argument("--map", default="Simple64")
        run.add_argument("--scenario", choices=["build_and_scout", "full_match", "expansion_logistics",
                                             "army_readiness", "expansion_and_victory"], default="build_and_scout")
        run.add_argument("--max-rounds", type=int, default=60)
        run.add_argument("--game-seconds", type=int, default=300)
        run.add_argument("--advance-loops", type=int, default=112)
        run.add_argument("--no-communication", action="store_true")
        run.add_argument("--no-guidance", action="store_true", help="关闭状态感知执行指导，用于框架对照")
        run.add_argument("--guidance-profile", choices=("none", "marine_reference_v1"), default="none",
                         help="可选给定Marine参考策略；属于LLM协同执行基线，不改变原生Victory验收")
        run.add_argument("--reasoning-budget-tokens", type=_reasoning_budget, default=0,
                         help="LLM 思考预算 0..1024；0 保持原非思考配置，正数使用固定思考实验配置")
        run.add_argument("--seeds", default="7" if name == "run" else "7,19,41")
        run.add_argument("--output", type=Path, default=Path("runtime/evaluation.json"))
    args = parser.parse_args()
    if args.command in (None, "serve"):
        import uvicorn

        from sc2_harness.app import create_app
        uvicorn.run(create_app(), host=getattr(args, "host", "127.0.0.1"),
                    port=getattr(args, "port", 18890), ws="websockets-sansio")
    elif args.command == "doctor":
        from sc2_harness.backends.native import NativeBackend
        from sc2_harness.settings import Settings
        print(json.dumps({"game": NativeBackend.probe(), "model": Settings.from_env().public_model(),
                          "mode": "read_only_no_inference"}, ensure_ascii=False, indent=2))
    else:
        records = []
        configs = [RunConfig(backend=args.backend, policy=args.policy, map_name=args.map,
                             scenario=args.scenario, seed=int(value), max_rounds=args.max_rounds,
                             max_game_seconds=args.game_seconds, communication=not args.no_communication,
                             advance_loops=args.advance_loops,
                             guidance_profile=args.guidance_profile,
                             reasoning_budget_tokens=args.reasoning_budget_tokens,
                             execution_guidance=not args.no_guidance)
                   for value in args.seeds.split(",")]
        _write_report(args, records, len(configs), complete=False)
        with httpx.Client(base_url=args.url, timeout=30, trust_env=False) as client:
            for config in configs:
                run = None
                try:
                    response = client.post("/api/runs", json=config.model_dump())
                    response.raise_for_status()
                    run = response.json()
                    print(f"Started {run['id']} backend={args.backend} policy={args.policy}", flush=True)
                    deadline = time.monotonic() + config.max_rounds * (config.decision_timeout + 95) + 240
                    while run["status"] not in TERMINAL:
                        if time.monotonic() >= deadline:
                            client.post(f"/api/runs/{run['id']}/control", json={"action": "stop"}).raise_for_status()
                            raise SystemExit("Evaluation exceeded its orchestration deadline; stop requested")
                        time.sleep(1)
                        response = client.get(f"/api/runs/{run['id']}")
                        response.raise_for_status()
                        run = response.json()
                except (httpx.HTTPError, ValueError, KeyError, KeyboardInterrupt, SystemExit) as exc:
                    records.append(_record(config, run, exc))
                    _write_report(args, records, len(configs), complete=False)
                    print(f"Partial report: {args.output}; server evidence remains unchanged", flush=True)
                    raise
                records.append(_record(config, run))
                _write_report(args, records, len(configs), complete=False)
                print(f"Finished {run['id']}: {run['result']}", flush=True)
        _write_report(args, records, len(configs), complete=True)
        print(f"Report: {args.output}")


if __name__ == "__main__":
    main()

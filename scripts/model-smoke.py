"""Exercise the local model's three role policies and JSON schema without starting any game."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx


async def run(policy_file: Path, output: Path, model: str = "Qwen3-4B") -> None:
    spec = importlib.util.spec_from_file_location("sc2_model_smoke_policies", policy_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    observation = {
        "engine": "model-smoke-fixture",
        "game_loop": 224,
        "game_time": 10,
        "player_id": 1,
        "minerals": 350,
        "vespene": 0,
        "supply_cap": 23,
        "supply_used": 16,
        "workers": 12,
        "army_count": 4,
        "structures": {"COMMANDCENTER": 1, "SUPPLYDEPOT": 1, "BARRACKS": 1},
        "unit_counts": {"SCV": 12, "MARINE": 4},
        "units": [
            {"tag": 1, "type": "COMMANDCENTER", "owner": "self", "is_ready": True, "is_idle": True},
            {"tag": 2, "type": "BARRACKS", "owner": "self", "is_ready": True, "is_idle": True},
            {"tag": 3, "type": "SCV", "owner": "self", "is_ready": True, "is_idle": True},
        ],
        "enemy_visible": [],
        "scouted": False,
        "map": {"width": 64, "height": 64, "start": [5, 5], "enemy_start": [55, 55]},
        "task": {
            "scenario": "build_and_scout",
            "objective": "Reach 14 workers, ready supply and barracks, "
            "four ready marines, and scout the enemy region.",
            "milestones": [
                {"name": "workers", "observed": 12, "target": 14, "done": False},
                {"name": "scout", "done": False},
            ],
        },
        "last_receipts": [],
    }
    policy = module.LLMPolicy(
        "http://127.0.0.1:18891/v1", model, timeout=120, max_output_tokens=384, history_turns=0
    )
    started = time.perf_counter()
    try:
        results = await asyncio.gather(
            *(policy.decide(role, observation, []) for role in module.ROLES), return_exceptions=True
        )
        roles = {}
        for role, result in zip(module.ROLES, results):
            if isinstance(result, BaseException):
                roles[role] = {
                    "ok": False,
                    "error": str(result),
                    "code": getattr(result, "code", None),
                    "request_count": getattr(result, "request_count", 0),
                    "usage": getattr(result, "usage", None),
                }
            else:
                roles[role] = {"ok": True, **result}
        report = {
            "kind": "local-model-api-smoke-with-fixture-not-game",
            "model": model,
            "roles": roles,
            "three_roles_wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "policy_sha256": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
            "accounting": policy.accounting,
        }
    finally:
        await policy.close()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ready", "model"],
        "properties": {
            "ready": {"type": "boolean", "const": True},
            "model": {"type": "string", "const": model},
        },
    }
    schema_started = time.perf_counter()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                "http://127.0.0.1:18891/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Return only " + json.dumps({"ready": True, "model": model}) + ".",
                        }
                    ],
                    "max_tokens": 64,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "local-model-health",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                },
                timeout=120,
            )
            response.raise_for_status()
            payload = response.json()
            parsed = json.loads(payload["choices"][0]["message"]["content"])
            report["json_schema"] = {
                "ok": parsed == {"ready": True, "model": model},
                "output": parsed,
                "usage": payload.get("usage"),
                "latency_ms": round((time.perf_counter() - schema_started) * 1000, 3),
            }
    except (OSError, ValueError, KeyError, httpx.HTTPError) as exc:
        report["json_schema"] = {"ok": False, "error": type(exc).__name__}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    summary = {
        "output": str(output),
        "three_roles_wall_ms": report["three_roles_wall_ms"],
        "roles": {
            role: {
                key: result.get(key)
                for key in (
                    "ok",
                    "goal",
                    "actions",
                    "usage",
                    "usage_reported",
                    "latency_ms",
                    "request_count",
                    "error",
                )
            }
            for role, result in roles.items()
        },
        "json_schema": report["json_schema"],
    }
    print(json.dumps(summary), flush=True)
    if not all(role["ok"] for role in roles.values()) or not report["json_schema"]["ok"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="Qwen3-4B")
    args = parser.parse_args()
    asyncio.run(run(args.policy_file.resolve(), args.output.resolve(), args.model))


if __name__ == "__main__":
    main()

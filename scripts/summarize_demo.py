"""Offline, observation-based three-map demo audit. No network, inference or game control."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from uuid import uuid4

TARGET_MAPS = ("Simple64", "Simple96", "AbyssalReefLE")
ROLES = ("economy", "production", "tactics")
CONFIG_KEYS = ("backend", "policy", "scenario", "seed", "max_rounds", "max_game_seconds", "advance_loops",
               "decision_timeout", "communication", "execution_guidance", "replay", "guidance_profile")
INFERENCE_KEYS = ("temperature", "enable_thinking", "reasoning_format", "reasoning_budget_tokens",
                  "max_output_tokens", "history_turns", "strict_schema", "context_window_tokens")
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_JSONL_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 100000


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def _native_results(run: dict) -> set[str]:
    values = (mapping(run.get("observation")).get("result"), mapping(run.get("metrics")).get("engine_result"),
              mapping(run.get("evaluation")).get("engine_result"))
    known = run.get("_known_native_results", [])
    values = (*values, *(known if isinstance(known, list) else []))
    return {value for value in values if isinstance(value, str) and value in {"Victory", "Defeat", "Tie"}}


def _inference_profiles(run: dict, events) -> tuple[list[dict], bool]:
    """Keep only recorded, public generation settings; absence never implies today's defaults."""
    known = run.get("_known_inference_settings")
    candidates = list(known) if isinstance(known, list) else []
    if run.get("inference_settings") is not None:
        candidates.append(run["inference_settings"])
    candidates.extend(mapping(event.get("data"))["inference_settings"] for event in events or []
                      if event.get("type") == "agent.decision"
                      and mapping(event.get("data")).get("inference_settings") is not None)
    profiles, invalid = {}, False
    for candidate in candidates:
        if not isinstance(candidate, dict) or not any(key in candidate for key in INFERENCE_KEYS):
            invalid = True
            continue
        profile = {key: candidate.get(key) for key in INFERENCE_KEYS}
        valid = all(profile[key] is None or isinstance(profile[key], bool)
                    for key in ("enable_thinking", "strict_schema")) \
            and (profile["temperature"] is None or number(profile["temperature"]) is not None) \
            and (profile["reasoning_format"] is None or profile["reasoning_format"] == "deepseek") \
            and all(profile[key] is None or integer(profile[key]) is not None
                    for key in ("reasoning_budget_tokens", "max_output_tokens", "history_turns")) \
            and (profile["context_window_tokens"] is None or
                 integer(profile["context_window_tokens"]) is not None and profile["context_window_tokens"] > 0)
        if not valid:
            invalid = True
            continue
        profiles[json.dumps(profile, sort_keys=True, allow_nan=False)] = profile
    return [profiles[key] for key in sorted(profiles)], invalid


def _read(path: Path, *, lines: bool, issues: list, source: str):
    try:
        if path.is_symlink() or path.stat().st_size > (MAX_JSONL_BYTES if lines else MAX_JSON_BYTES):
            raise ValueError("unsafe_or_oversized_file")
        if not lines:
            with path.open("rb") as stream:
                raw = stream.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("json_size_limit")
            value = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise TypeError("expected_object")
            return value
        result, total = [], 0
        with path.open("rb") as stream:
            while True:
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                total += len(raw)
                if len(raw) > MAX_LINE_BYTES or total > MAX_JSONL_BYTES or len(result) >= MAX_ROWS:
                    raise ValueError("jsonl_limit")
                if not raw.strip():
                    continue
                value = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(value, dict):
                    raise TypeError("expected_jsonl_object")
                result.append(value)
        return result
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        issues.append({"source": source, "file": path.name,
                       "code": "missing_file" if isinstance(exc, FileNotFoundError) else type(exc).__name__})
        return None


def _load(evidence_dirs, report_paths):
    records, issues, report_metadata = [], [], []
    for directory in map(Path, evidence_dirs):
        source = f"evidence:{directory.name}"
        files = {name: _read(directory / name, lines=name.endswith("jsonl"), issues=issues, source=source)
                 for name in ("run.json", "config.json", "metrics.json", "events.jsonl", "snapshots.jsonl")}
        run = files["run.json"]
        if run is None:
            continue
        run = dict(run)
        if files["config.json"] is not None:
            if run.get("config") is not None and run["config"] != files["config.json"]:
                run["_conflicts"] = ["config_artifact_disagrees"]
            run["config"] = files["config.json"]
        if files["metrics.json"] is not None:
            if run.get("metrics") is not None and run["metrics"] != files["metrics.json"]:
                run.setdefault("_conflicts", []).append("metrics_artifact_disagrees")
            run["metrics"] = files["metrics.json"]
        records.append({"run": run, "events": files["events.jsonl"], "snapshots": files["snapshots.jsonl"],
                        "sources": [source], "kind": "evidence"})
    for path in map(Path, report_paths):
        source = f"report:{path.name}"
        report = _read(path, lines=False, issues=issues, source=source)
        if report is None:
            continue
        runs = report.get("runs")
        if not isinstance(runs, list) or len(runs) > 10000:
            issues.append({"source": source, "file": path.name, "code": "invalid_report_runs"})
            continue
        report_metadata.append({"source": source, "complete": report.get("complete"),
                                "planned_sample_count": integer(report.get("planned_sample_count")),
                                "record_count": len(runs)})
        for index, run in enumerate(runs):
            if not isinstance(run, dict):
                issues.append({"source": source, "file": path.name, "code": "invalid_report_record"})
                continue
            records.append({"run": run, "events": None, "snapshots": None,
                            "sources": [f"{source}#{index}"], "kind": "report"})
    # The same run appears in reports and exports. It is one attempt, not several independent samples.
    unique = {}
    for index, record in enumerate(records):
        identity = record["run"].get("id")
        key = identity if isinstance(identity, str) and re.fullmatch(r"[0-9a-fA-F]{32}", identity) else f"unknown:{index}"
        if key not in unique:
            unique[key] = record
            continue
        old = unique[key]
        primary, secondary = (record, old) if record["kind"] == "evidence" else (old, record)
        primary["run"]["_conflicts"] = list(primary["run"].get("_conflicts", [])) + list(
            secondary["run"].get("_conflicts", []))
        first_results, second_results = _native_results(primary["run"]), _native_results(secondary["run"])
        primary["run"]["_known_native_results"] = sorted(first_results | second_results)
        if first_results and second_results and first_results != second_results:
            primary["run"]["_conflicts"].append("duplicate_native_terminal_disagrees")
        first_profiles, first_invalid = _inference_profiles(primary["run"], primary["events"])
        second_profiles, second_invalid = _inference_profiles(secondary["run"], secondary["events"])
        primary["run"]["_known_inference_settings"] = first_profiles + second_profiles
        if first_invalid or second_invalid:
            primary["run"]["_conflicts"].append("invalid_inference_settings")
        for field in ("status", "result", "model_name", "source_fingerprint", "config"):
            a, b = primary["run"].get(field), secondary["run"].get(field)
            if a is not None and b is not None and a != b:
                primary["run"].setdefault("_conflicts", []).append(f"duplicate_{field}_disagrees")
        for field, value in secondary["run"].items():
            if primary["run"].get(field) is None:
                primary["run"][field] = value
        primary["sources"] = sorted(set(primary["sources"] + secondary["sources"]))
        unique[key] = primary
    return list(unique.values()), issues, report_metadata


def _role_stats(run: dict, events) -> dict:
    agents = run.get("agents")
    agents = agents if isinstance(agents, list) else []
    result = {}
    for role in ROLES:
        agent = next((item for item in agents if isinstance(item, dict) and item.get("id") == role), {})
        stats = mapping(agent.get("stats"))
        calls = integer(stats.get("calls"))
        if calls is None and events is not None:
            requests = [mapping(event.get("data")).get("request_count") for event in events
                        if event.get("actor_id") == role and event.get("type") in {"agent.decision", "agent.error"}]
            if requests and all(integer(value) is not None for value in requests):
                calls = sum(requests)
        counts = None if events is None else Counter(event.get("type") for event in events
                                                    if event.get("actor_id") == role)
        tools = None if events is None else Counter(mapping(event.get("data")).get("tool") for event in events
                    if event.get("actor_id") == role and event.get("type") == "command.proposed"
                    and isinstance(mapping(event.get("data")).get("tool"), str))
        result[role] = {"model_calls": calls, "input_tokens": number(stats.get("input_tokens")),
                        "output_tokens": number(stats.get("output_tokens")),
                        "event_counts": {name: counts.get(event_type, 0) if counts is not None else None
                                         for name, event_type in (
                            ("proposed", "command.proposed"), ("accepted", "command.accepted"),
                            ("initial_rejected", "command.rejected"), ("execution_failed", "command.execution_failed"),
                            ("execution_partial", "command.execution_partial"), ("messages_sent", "agent.message"))},
                        "proposed_tools": dict(tools) if tools is not None else None}
    return result


def _tokens(metrics: dict, roles: dict) -> dict:
    result = {}
    ledger = [roles[role] for role in ROLES]
    aligned = all(item["model_calls"] is not None for item in ledger) and integer(metrics.get("model_calls")) is not None \
        and sum(item["model_calls"] for item in ledger) == metrics["model_calls"]
    for field in ("input_tokens", "output_tokens"):
        value = number(metrics.get(field))
        source = "reported_metrics" if value is not None else None
        if metrics.get(field) is None and aligned and all(item["model_calls"] == 0 or item[field] is not None
                                                        for item in ledger):
            value = sum(item[field] if item["model_calls"] else 0 for item in ledger)
            source = "derived_role_ledger"
        result[field], result[f"{field}_source"] = value, source
    return result


def _call_evidence(events, roles: dict) -> tuple[dict, list[str]]:
    """Cross-check HTTP attempts without treating metadata as proof of completed LLM decisions."""
    evidence, conflicts = {}, []
    for role in ROLES:
        relevant = [event for event in events or [] if event.get("actor_id") == role
                    and event.get("type") in {"agent.decision", "agent.error"}]
        counts = [integer(mapping(event.get("data")).get("request_count")) for event in relevant]
        event_total = sum(counts) if events is not None and all(value is not None for value in counts) else None
        decisions = [mapping(event.get("data")) for event in relevant if event.get("type") == "agent.decision"]
        contradictory = any(isinstance(item.get("policy"), str) and item["policy"] != "llm" for item in decisions)
        positive = sum(integer(item.get("request_count")) or 0 for item in decisions if item.get("policy") == "llm")
        incomplete = events is None or any(item.get("policy") is None or integer(item.get("request_count")) is None
                                           for item in decisions)
        proof = False if contradictory else True if positive > 0 else None if incomplete else False
        ledger = roles[role]["model_calls"]
        if ledger is None or event_total is None:
            consistency, coverage = None, "unknown_request_coverage"
        elif ledger == event_total:
            consistency, coverage = True, "aligned"
        elif ledger > event_total:
            consistency, coverage = None, "ledger_exceeds_recorded_events_possible_inflight_or_partial_trace"
        else:
            consistency, coverage = False, "recorded_events_exceed_ledger"
            conflicts.append(f"{role}_event_requests_exceed_ledger")
        if contradictory:
            conflicts.append(f"{role}_non_llm_decision_policy")
        evidence[role] = {"ledger_calls": ledger, "recorded_event_requests": event_total,
                          "positive_llm_decision_requests": positive if events is not None else None,
                          "llm_call_proven": proof, "ledger_consistent": consistency, "coverage": coverage}
    return evidence, conflicts


def _all_checks(values) -> bool | None:
    return False if any(value is False for value in values) else True if all(value is True for value in values) else None


def _peaks(snapshots) -> dict:
    if snapshots is None:
        return {"ready_army": None, "ready_marines": None, "snapshots": None}
    armies, marines = [], []
    workers = {"SCV", "MULE", "PROBE", "DRONE"}
    for snapshot in snapshots:
        observation = mapping(snapshot.get("observation"))
        ready = observation.get("ready_counts")
        if isinstance(ready, dict):
            count = integer(ready.get("MARINE", 0))
            if count is not None:
                marines.append(count)
        units = observation.get("units")
        if not isinstance(units, list):
            continue
        if any(not isinstance(unit, dict) or unit.get("owner") not in ("self", "enemy") for unit in units):
            continue
        own = [unit for unit in units if isinstance(unit, dict) and unit.get("owner") == "self"]
        if any(not isinstance(unit.get("is_ready"), bool) or not isinstance(unit.get("is_structure"), bool)
               or not isinstance(unit.get("type"), str) for unit in own):
            continue
        army = [unit for unit in own if unit["is_ready"] and not unit["is_structure"] and unit["type"] not in workers]
        armies.append(len(army))
        if not isinstance(ready, dict):
            marines.append(sum(unit["type"] == "MARINE" for unit in army))
    return {"ready_army": max(armies) if armies else None, "ready_marines": max(marines) if marines else None,
            "snapshots": len(snapshots), "scope": "maximum in supplied snapshots, not an unsampled whole-game peak"}


def _check(value, expected):
    return None if value is None else value == expected


def _project(record: dict) -> dict:
    run, events = record["run"], record["events"]
    config, metrics, observation = mapping(run.get("config")), mapping(run.get("metrics")), mapping(run.get("observation"))
    roles = _role_stats(run, events)
    models = {mapping(event.get("data")).get("model") for event in events or []
              if event.get("type") == "agent.decision" and isinstance(mapping(event.get("data")).get("model"), str)}
    model = run.get("model_name")
    if not isinstance(model, str) or not model.strip():
        model = next(iter(models)) if len(models) == 1 else None
    conflicts = list(run.get("_conflicts", []))
    call_evidence, call_conflicts = _call_evidence(events, roles)
    conflicts.extend(call_conflicts)
    inference_profiles, invalid_inference = _inference_profiles(run, events)
    inference_settings = inference_profiles[0] if len(inference_profiles) == 1 else None
    if invalid_inference:
        conflicts.append("invalid_inference_settings")
    if len(inference_profiles) > 1:
        conflicts.append("inference_settings_disagree")
    if model is not None and models and models != {model}:
        conflicts.append("historical_model_identity_disagrees")
    fingerprint = run.get("source_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
        fingerprint = None
    elif fingerprint is not None:
        fingerprint = fingerprint.lower()
    native_values = _native_results(run)
    engine_result = next(iter(native_values)) if len(native_values) == 1 else None
    if len(native_values) > 1:
        conflicts.append("native_terminal_disagrees")
    native = _check(observation.get("engine"), "sc2") if observation.get("engine") is not None \
        else run.get("native_game_evidence") if isinstance(run.get("native_game_evidence"), bool) else None
    identity = mapping(observation.get("engine_identity"))
    opponent = identity.get("opponent") if isinstance(identity.get("opponent"), str) else None
    native_map = identity.get("map_name") if isinstance(identity.get("map_name"), str) else None
    declared_map = config.get("map_name", run.get("map_name"))
    if native_map is not None and declared_map is not None and native_map != declared_map:
        conflicts.append("native_map_disagrees_with_config")
    if config.get("map_name") is not None and run.get("map_name") is not None \
            and config["map_name"] != run["map_name"]:
        conflicts.append("run_map_disagrees_with_config")
    wall = number(metrics.get("wall_seconds"))
    calls = [roles[role]["model_calls"] for role in ROLES]
    if all(value is not None for value in calls) and integer(metrics.get("model_calls")) is not None \
            and sum(calls) != metrics["model_calls"]:
        conflicts.append("global_role_calls_disagree")
    run_identity = run.get("id")
    checks = {"run_identity": bool(re.fullmatch(r"[0-9a-fA-F]{32}", run_identity))
              if isinstance(run_identity, str) else None,
              "native_sc2": native, "backend_sc2": _check(config.get("backend"), "sc2"),
              "llm_policy": _check(config.get("policy"), "llm"),
              "qwen3_model": bool(re.search(r"(?:^|[/_\s-])qwen3(?:$|[/_.\s-])", model, re.IGNORECASE))
              if model else None,
              "full_match": _check(config.get("scenario"), "full_match"),
              "native_victory": _check(engine_result, "Victory"),
              "computer_opponent": bool(re.search(r"builtin|computer", opponent, re.IGNORECASE))
              if opponent else None,
              "native_map_matches_config": _check(native_map, declared_map) if declared_map is not None else None,
              "wall_at_least_300s": wall >= 300 if wall is not None else None,
              "all_three_roles_called": all(value > 0 for value in calls) if all(value is not None for value in calls)
              else None,
              "actual_llm_call_evidence": _all_checks([call_evidence[role]["llm_call_proven"] for role in ROLES]),
              "role_call_ledgers_consistent": _all_checks([call_evidence[role]["ledger_consistent"] for role in ROLES]),
              "completed": _check(run.get("status"), "completed"),
              "mission_success": _check(run.get("result"), "success")}
    core_config = {key: config.get(key) for key in CONFIG_KEYS}
    config_complete = all(value is not None for key, value in core_config.items() if key != "guidance_profile")
    config_complete = config_complete and integer(core_config["seed"]) is not None \
        and all(integer(core_config[key]) is not None and core_config[key] > 0
                for key in ("max_rounds", "max_game_seconds", "advance_loops")) \
        and number(core_config["decision_timeout"]) is not None and core_config["decision_timeout"] > 0 \
        and all(isinstance(core_config[key], bool) for key in ("communication", "execution_guidance", "replay"))
    guidance_profile = config.get("guidance_profile")
    if guidance_profile is not None and (not isinstance(guidance_profile, str) or
                                        guidance_profile not in {"none", "marine_reference_v1"}):
        conflicts.append("invalid_guidance_profile")
    if guidance_profile == "marine_reference_v1" and (config.get("scenario") != "full_match" or
            config.get("policy") != "llm" or config.get("execution_guidance") is not True):
        conflicts.append("guidance_profile_config_disagrees")
    # Optional historical metadata must separate cohorts without invalidating older configurations.
    reasoning_budget = config.get("reasoning_budget_tokens")
    core_config["reasoning_budget_tokens"] = reasoning_budget
    if reasoning_budget is not None and (integer(reasoning_budget) is None or reasoning_budget > 1024):
        conflicts.append("invalid_reasoning_budget_tokens")
    if inference_settings is not None and integer(reasoning_budget) is not None:
        actual_budget = inference_settings["reasoning_budget_tokens"]
        thinking = inference_settings["enable_thinking"]
        if (actual_budget is not None and actual_budget != reasoning_budget) \
                or (reasoning_budget == 0 and thinking is True) \
                or (reasoning_budget > 0 and thinking is False):
            conflicts.append("reasoning_budget_disagrees_with_inference_settings")
    if native is True and opponent is None:
        conflicts_for_comparison = ["opponent_not_recorded"]
    else:
        conflicts_for_comparison = []
    comparable = fingerprint is not None and model is not None and config_complete
    cohort = {"source_fingerprint": fingerprint, "model_name": model, "config": core_config, "opponent": opponent,
              "game_version": identity.get("game_version") if isinstance(identity.get("game_version"), str) else None,
              "base_build": integer(identity.get("base_build")), "inference_settings": inference_settings}
    return {"run_id": run.get("id"), "sources": record["sources"], "status": run.get("status"),
            "mission_result": run.get("result") if isinstance(run.get("result"), str) else None,
            "engine_result": engine_result, "native_results_observed": sorted(native_values),
            "map_name": declared_map, "native_map_name": native_map, "seed": config.get("seed"),
            "guidance_profile": guidance_profile,
            "model_name": model, "source_fingerprint": fingerprint, "evaluation_version": run.get("evaluation_version"),
            "wall_seconds": wall, "game_seconds": number(observation.get("game_time", metrics.get("game_seconds"))),
            "checks": checks, "unknown_checks": [key for key, value in checks.items() if value is None],
            "failed_checks": [key for key, value in checks.items() if value is False],
            "qualifying_win": False if conflicts else _all_checks(list(checks.values())),
            "conflicts": sorted(set(conflicts)), "comparison_warnings": conflicts_for_comparison,
            "cohort": cohort, "comparison_complete": comparable, "roles": roles, "call_evidence": call_evidence,
            "inference_settings": inference_settings, "inference_settings_observed": inference_profiles,
            "events_available": events is not None, "peaks": _peaks(record["snapshots"]),
            "tokens": _tokens(metrics, roles),
            "metrics": {key: number(metrics.get(key)) for key in (
                "model_calls", "model_errors", "commands_proposed", "commands_accepted", "commands_rejected",
                "commands_execution_failed", "commands_execution_partial", "latency_p50_ms", "latency_p95_ms",
                "round_decision_latency_p50_ms", "round_decision_latency_p95_ms")}}


def _rates(attempts: list[dict]) -> dict:
    wins = sum(attempt["engine_result"] == "Victory" and attempt["checks"]["native_sc2"] is True
               and not any("native_terminal_disagrees" in conflict for conflict in attempt["conflicts"])
               for attempt in attempts)
    return {"attempt_records": len(attempts), "confirmed_native_victories": wins,
            "qualifying_demo_wins": sum(attempt["qualifying_win"] is True for attempt in attempts),
            "native_win_rate_over_all_supplied_attempts": wins / len(attempts) if attempts else None,
            "scope": "all successfully parsed supplied records after run-ID deduplication, including errors/unknowns; "
                     "not a claim that every historical attempt was supplied",
            "mission_results": dict(Counter(attempt["mission_result"] or "unknown" for attempt in attempts)),
            "native_results": dict(Counter(attempt["engine_result"] or "unknown" for attempt in attempts))}


def summarize_demo(evidence_dirs=(), report_paths=(), *, fingerprint=None, model_name=None) -> dict:
    if fingerprint is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
        raise ValueError("fingerprint must be 64 hexadecimal characters")
    records, issues, reports = _load(evidence_dirs, report_paths)
    attempts = [_project(record) for record in records]
    groups = {}
    for attempt in attempts:
        key = json.dumps(attempt["cohort"], ensure_ascii=False, sort_keys=True, allow_nan=False)
        groups.setdefault(key, []).append(attempt)
    cohorts = []
    for members in groups.values():
        identity = members[0]["cohort"]
        selected = (fingerprint is None or identity["source_fingerprint"] == fingerprint.lower()) \
            and (model_name is None or identity["model_name"] == model_name)
        maps = {name: {"attempts": sum(item["map_name"] == name for item in members),
                       "qualifying_run_ids": [item["run_id"] for item in members
                                              if item["map_name"] == name and item["qualifying_win"]]}
                for name in TARGET_MAPS}
        cohorts.append({"identity": identity, "selected": selected, "comparison_complete": all(
            item["comparison_complete"] for item in members), "rates": _rates(members), "maps": maps,
            "demo_pass": selected and not issues and all(item["comparison_complete"] for item in members)
                         and all(item["qualifying_run_ids"] for item in maps.values())})
    return {"format_version": 1, "target_maps": list(TARGET_MAPS), "minimum_wall_seconds": 300,
            "selection": {"fingerprint": fingerprint, "model_name": model_name}, "attempts": attempts,
            "rates": _rates(attempts), "cohorts": cohorts, "demo_pass": any(group["demo_pass"] for group in cohorts),
            "input_issues": issues, "report_metadata": reports,
            "notes": ["A specialized mission success is not a native Victory; three seeds on one map are not three maps.",
                      "Identity is recorded historical metadata, not independent verification of model weights or source.",
                      ("Strict demo qualification checks the native map and positive LLM decision requests per role. "
                       "Missing event proof or ledger requests exceeding recorded turns are unknown, including possible "
                       "cancelled/in-flight requests; totals are not silently rewritten."),
                      "Same-cohort configuration includes seed and budgets; only map_name and descriptive name differ.",
                      ("Guidance profile is run configuration, not a model inference setting: legacy unknown, explicit "
                       "none and marine_reference_v1 remain separate cohorts. Reference soft targets do not replace "
                       "native Victory qualification."),
                      "Known opponents/builds separate cohorts; missing opponent metadata is explicitly reported.",
                      "Partial progress, missing usage and missing latency remain unknown; role events are counts by type.",
                      "Per-run latency quantiles are reported as supplied, never averaged into pooled quantiles.",
                      "Snapshots give sampled peaks; model waits pause both sides, and wall time is not game time."]}


def write_summary(path: Path, summary: dict, *, evidence_dirs=(), report_paths=()) -> None:
    output = path.resolve()
    if output.exists() or path.is_symlink() or any(output.is_relative_to(Path(directory).resolve())
                                                 for directory in evidence_dirs) \
            or output in {Path(report).resolve() for report in report_paths}:
        raise ValueError("output must be a new file outside the original evidence directories/reports")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(summary, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if output.exists():
            raise ValueError("output appeared during summarization; refusing overwrite")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线核对三图原生Qwen3完整对局，不启动模型或游戏")
    parser.add_argument("--evidence-dir", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--fingerprint", help="可选：只允许指定的冻结源码SHA256通过；其他尝试仍列出")
    parser.add_argument("--model-name", help="可选：只允许指定历史模型身份通过")
    parser.add_argument("--output", type=Path, help="新JSON文件，必须在原始证据目录之外；默认JSON输出到stdout")
    args = parser.parse_args()
    if not args.evidence_dir and not args.report:
        parser.error("provide at least one --evidence-dir or --report")
    try:
        summary = summarize_demo(args.evidence_dir, args.report, fingerprint=args.fingerprint, model_name=args.model_name)
        if args.output:
            write_summary(args.output, summary, evidence_dirs=args.evidence_dir, report_paths=args.report)
            print(json.dumps({"demo_pass": summary["demo_pass"], "attempts": len(summary["attempts"]),
                              "input_issues": len(summary["input_issues"]), "output": args.output.name}))
        else:
            print(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Offline summary failed: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()

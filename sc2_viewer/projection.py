"""Bounded public projections. Unknown fields never cross the spectator boundary."""
from __future__ import annotations

import math
import re

_SECRET = re.compile(r"(?i)(?:bearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|"
                     r"(?:api[_ -]?key|password|authorization|access[_ -]?token|secret)\s*[:=]\s*\S+)")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/][^\s\"'<>]+|(?<![\w:])/(?:home|root|tmp|Users|var|opt|mnt|srv|etc)/[^\s\"'<>]+)")
_URL = re.compile(r"https?://[^\s\"'<>]+")
_ENUM = re.compile(r"^[A-Za-z][A-Za-z0-9_ .-]{0,79}$")


def text(value, limit=1200):
    if not isinstance(value, str):
        return None
    return _URL.sub("[private URL]", _PATH.sub("[private path]", _SECRET.sub("[redacted]", value)))[:limit]


def scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    if isinstance(value, str):
        return text(value)
    return None


def fields(value, keys):
    return {key: scalar(value[key]) for key in keys if key in value} if isinstance(value, dict) else {}


def counts(value):
    return {key: scalar(item) for key, item in list(value.items())[:128]
            if isinstance(key, str) and _ENUM.fullmatch(key) and isinstance(item, (int, float))
            and not isinstance(item, bool) and math.isfinite(item)} if isinstance(value, dict) else {}


def point(value):
    if isinstance(value, (list, tuple)) and len(value) == 2 \
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return list(value)
    return None


def orders(value):
    result = []
    for item in value[:12] if isinstance(value, list) else []:
        row = fields(item, ("ability_id", "ability", "progress", "target_unit_tag", "produces", "researches"))
        if isinstance(item, dict) and "target" in item:
            row["target"] = point(item["target"])
        if row:
            result.append(row)
    return result


def unit(value):
    row = fields(value, ("tag", "type", "x", "y", "health", "health_max", "shield", "owner", "owner_id",
                         "is_structure", "is_idle", "is_ready", "build_progress", "assigned_harvesters",
                         "ideal_harvesters", "is_busy", "queue_length"))
    if isinstance(value, dict):
        row["orders"] = orders(value.get("orders"))
    return row


def _public_unit(value):
    # Preserve the native POV. A future upstream must not accidentally turn
    # remembered/snapshot enemy units into a current-visible spectator feed.
    return isinstance(value, dict) and not (value.get("owner") == "enemy" and (
        value.get("visible") is False or value.get("display_type") in {2, 3, 4}))


def action(value):
    if not isinstance(value, dict):
        return {}
    args = fields(value.get("args"), ("resource", "max_workers", "unit_tag", "x", "y", "count", "target_tag"))
    return {"tool": text(value.get("tool"), 64), "args": args}


def actions(value):
    return [action(item) for item in value[:32] if isinstance(item, dict)] if isinstance(value, list) else []


def message(value):
    return fields(value, ("from", "to", "sender", "recipient", "actor_id", "role", "text", "round", "time"))


def plan(value):
    if not isinstance(value, dict):
        return None
    row = fields(value, ("profile", "version", "revision", "label", "advisory", "phase", "focus", "scope",
                         "phase_entered_game_time", "reference_size_reached_at_game_time", "observed_at_game_time"))
    for key in ("soft_targets", "role_targets"):
        if isinstance(value.get(key), list):
            row[key] = [fields(item, ("id", "role", "tool", "target", "observed", "known", "met", "gap",
                                     "progress", "observed_source")) for item in value[key][:12]]
    hint = value.get("initial_hint_public_progress")
    if isinstance(hint, dict):
        row["initial_hint_public_progress"] = fields(hint, ("radius", "army_ready_total", "known_total",
            "near_count", "coverage", "positions_complete", "majority_near", "initial_hint_reached",
            "reached_at_game_time", "observed_at_game_time", "scope"))
        row["initial_hint_public_progress"]["hint_position"] = point(hint.get("hint_position"))
    return row


def observation(value):
    if not isinstance(value, dict):
        return {}
    row = fields(value, ("engine", "game_loop", "game_time", "player_id", "minerals", "vespene",
                         "supply_used", "supply_cap", "workers", "army_count", "enemy_visible", "scouted",
                         "result", "rendering"))
    for key in ("structures", "unit_counts", "ready_counts", "pending_counts", "queued_counts", "score"):
        row[key] = counts(value.get(key))
    row["units"] = [unit(item) for item in value.get("units", [])[:1024] if _public_unit(item)] \
        if isinstance(value.get("units"), list) else []
    row["production"] = [unit(item) for item in value.get("production", [])[:128] if isinstance(item, dict)] \
        if isinstance(value.get("production"), list) else []
    map_data = value.get("map", {})
    row["map"] = fields(map_data, ("width", "height"))
    if isinstance(map_data, dict):
        for key in ("start", "enemy_start"):
            row["map"][key] = point(map_data.get(key))
        row["map"]["expansions"] = [point(p) for p in map_data.get("expansions", [])[:64] if point(p)] \
            if isinstance(map_data.get("expansions"), list) else []
    row["engine_identity"] = fields(value.get("engine_identity"), ("game_version", "base_build", "data_build",
        "sdk", "map_name", "map_sha256", "native_player_count", "opponent", "clock"))
    upgrades = value.get("upgrades")
    if isinstance(upgrades, dict):
        row["upgrades"] = {"completed": [text(v, 80) for v in upgrades.get("completed", [])[:64]
                                        if isinstance(v, str)]}
    return row


CONFIG_KEYS = ("name", "backend", "policy", "scenario", "map_name", "seed", "max_rounds", "max_game_seconds",
               "advance_loops", "decision_timeout", "communication", "execution_guidance", "guidance_profile",
               "reasoning_budget_tokens", "replay")
METRIC_KEYS = ("model_calls", "model_errors", "input_tokens", "output_tokens", "commands_proposed",
               "commands_accepted", "commands_rejected", "commands_execution_failed", "commands_execution_partial",
               "latency_p50", "latency_p95", "round_decision_latency_p50", "round_decision_latency_p95",
               "latency_p50_ms", "latency_p95_ms", "round_decision_latency_p50_ms",
               "round_decision_latency_p95_ms", "game_seconds", "wall_seconds", "engine_result",
               "command_acceptance_rate", "command_rejection_rate", "milestones_achieved", "milestones_total",
               "milestone_completion_rate", "stages_completed", "stages_total", "stage_completion_rate",
               "task_completion_game_seconds", "task_completion_wall_seconds", "task_success",
               "role_decisions_scheduled", "empty_action_decisions")


def evaluation(value):
    if not isinstance(value, dict):
        return {}
    row = fields(value, ("success", "result", "passed", "complete", "reason", "engine_result", "scenario",
                         "evaluation_version", "outcome", "progress", "termination_reason", "current_stage",
                         "observed_at_game_time", "censored"))
    for key in ("checks", "criteria"):
        data = value.get(key)
        if isinstance(data, dict):
            row[key] = {name: scalar(v) for name, v in list(data.items())[:32]
                        if isinstance(name, str) and _ENUM.fullmatch(name)
                        and isinstance(v, (bool, int, float, type(None)))}
        elif isinstance(data, list):
            row[key] = [fields(item, ("id", "label", "passed", "met", "observed", "target", "known"))
                        for item in data[:32]]
    return row


def run(value):
    row = fields(value, ("id", "name", "status", "created_at", "updated_at", "round", "backend", "policy",
                         "scenario", "map_name", "result", "model_name", "source_fingerprint", "evaluation_version"))
    if not isinstance(value, dict):
        return row
    row["config"] = fields(value.get("config"), CONFIG_KEYS)
    row["observation"] = observation(value.get("observation"))
    row["metrics"] = fields(value.get("metrics"), METRIC_KEYS)
    if isinstance(value.get("metrics"), dict) and isinstance(value["metrics"].get("native_score"), dict):
        native = value["metrics"]["native_score"]
        row["metrics"]["native_score"] = counts(native)
        row["metrics"]["native_score"].update(fields(native, ("source", "available", "score_type")))
    row["evaluation"] = evaluation(value.get("evaluation"))
    row["reference_plan"] = plan(value.get("reference_plan"))
    row["has_error"] = bool(value.get("error"))
    row["agents"] = []
    for item in value.get("agents", [])[:3] if isinstance(value.get("agents"), list) else []:
        if not isinstance(item, dict) or item.get("id") not in {"economy", "production", "tactics"}:
            continue
        agent = fields(item, ("id", "name", "status", "goal", "last_reason"))
        agent["last_goal"] = agent.get("goal")
        agent["last_actions"] = actions(item.get("last_actions"))
        agent["stats"] = fields(item.get("stats"), ("calls", "errors", "input_tokens", "output_tokens"))
        agent["messages"] = [message(msg) for msg in item.get("messages", [])[:32]] \
            if isinstance(item.get("messages"), list) else []
        row["agents"].append(agent)
    row["milestones"] = [fields(item, ("id", "label", "met", "passed", "game_time", "round", "value", "target",
                                      "known", "achieved", "reported", "achieved_at_game_time", "censored"))
                         for item in value.get("milestones", [])[:32]] \
        if isinstance(value.get("milestones"), list) else []
    return row


def event(value):
    row = fields(value, ("seq", "type", "time", "round", "actor_id", "message"))
    data = value.get("data") if isinstance(value, dict) else None
    row["data"] = fields(data, ("goal", "reason", "tool", "command_id", "status", "result", "text", "to",
                                "from", "recipient", "sender", "execution_status", "game_time", "round"))
    if isinstance(data, dict):
        if "actions" in data:
            row["data"]["actions"] = actions(data["actions"])
        if "args" in data:
            row["data"]["args"] = action(data)["args"]
        if isinstance(data.get("messages"), list):
            row["data"]["messages"] = [message(item) for item in data["messages"][:32]]
    return row


def snapshot(value):
    row = fields(value, ("seq", "time", "round"))
    row["observation"] = observation(value.get("observation")) if isinstance(value, dict) else {}
    row["reference_plan"] = plan(value.get("reference_plan")) if isinstance(value, dict) else None
    return row


def recording_manifest(value):
    row = fields(value, ("schema", "version", "kind", "source_run", "run_id", "status", "complete",
        "frame_count", "frames_saved", "capture_interval_seconds", "duration_seconds", "wall_seconds",
        "game_seconds", "started_at", "finished_at", "recording_sha256", "recording_bytes", "width", "height",
        "format_version", "recording_type", "unique_frames", "sampler_elapsed_wall_seconds", "interval_seconds",
        "started_utc", "ended_utc", "stop_reason"))
    if isinstance(value, dict) and isinstance(value.get("animation"), dict):
        row["animation"] = fields(value["animation"], ("encoded", "frame_count", "duration_ms", "bytes", "sha256"))
    return row

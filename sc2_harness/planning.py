"""Truth-only decision context; no recommended commands or hidden gameplay policy."""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from typing import Any

ROLES = ("economy", "production", "tactics")
ROLE_TOOLS = {
    "economy": ("train_worker", "gather", "build_supply", "build_refinery", "expand"),
    "production": ("build_barracks", "build_engineering_bay", "train_marine", "research_infantry"),
    "tactics": ("scout", "attack", "retreat", "hold"),
}


def _counts(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        str(name)[:48]: count
        for name, count in list(value.items())[:48]
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }


def _tags(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(tag for tag in value if isinstance(tag, int) and not isinstance(tag, bool) and tag > 0)
    )


def recent_execution_failures(role: str, receipts: Any) -> list[dict]:
    """Summarize observed effects separately from the unchanged initial ACK."""
    if not isinstance(receipts, list):
        return []
    summaries = []
    for receipt in receipts[-8:]:
        if (
            not isinstance(receipt, dict)
            or receipt.get("actor_id", role) != role
            or receipt.get("status") not in ("accepted", "completed")
        ):
            continue
        details = receipt.get("details")
        if not isinstance(details, dict):
            continue
        raw_orders = details.get("orders", [])
        orders = (
            [details, *(order for order in raw_orders if isinstance(order, dict))]
            if isinstance(raw_orders, list)
            else [details]
        )
        pairs = {
            (tag, order.get("ability_id"))
            for order in orders
            for tag in _tags(order.get("unit_tags"))
            if isinstance(order.get("ability_id"), int) and not isinstance(order["ability_id"], bool)
        }
        matched = []
        for key in ("observed_action_errors", "native_errors"):
            errors = details.get(key, [])
            for error in errors[:16] if isinstance(errors, list) else []:
                if not isinstance(error, dict):
                    continue
                if "command_id" in error:
                    matches = bool(receipt.get("command_id")) and error["command_id"] == receipt["command_id"]
                else:
                    matches = (
                        all(
                            isinstance(error.get(key), int) and not isinstance(error[key], bool)
                            for key in ("unit_tag", "ability_id")
                        )
                        and (error["unit_tag"], error["ability_id"]) in pairs
                    )
                if matches:
                    item = {
                        key: error[key]
                        for key in ("unit_tag", "ability_id", "result")
                        if isinstance(error.get(key), int) and not isinstance(error[key], bool)
                    }
                    if isinstance(error.get("result_name"), str):
                        item["result_name"] = error["result_name"][:80]
                    if item not in matched:
                        matched.append(item)
        execution = details.get("execution_status")
        if execution not in ("failed", "partial") and not matched:
            continue
        summary = {
            "command_id": str(receipt.get("command_id", ""))[:64],
            "tool": str(receipt.get("tool", ""))[:48],
            "ack_status": receipt["status"],
            "execution_status": execution if execution in ("failed", "partial") else "observed_native_error",
            "native_errors": matched[:4],
        }
        target = details.get("target")
        if isinstance(target, (list, tuple)) and len(target) == 2 and all(_finite(value) for value in target):
            summary["target"] = list(target)
        summaries.append(summary)
    return summaries[-4:]


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _enemy_visible(unit: dict, *, native_visible_only: bool) -> bool | None:
    """Native public units are prefiltered; older adapters need explicit visibility.

    A supplied hidden/snapshot flag always excludes a target, including when a
    caller accidentally mixes raw snapshots into the native public observation.
    """
    display = unit.get("display_type")
    if isinstance(display, int) and not isinstance(display, bool) and display in (2, 3, 4):
        return False
    if "visible" in unit:
        return unit["visible"] if isinstance(unit["visible"], bool) else None
    if "display_type" in unit:
        return True if isinstance(display, int) and not isinstance(display, bool) and display == 1 else None
    return True if native_visible_only else None


def _enemy_target(unit: dict) -> dict:
    tag = unit.get("tag")
    result = {"tag": tag if isinstance(tag, int) and not isinstance(tag, bool) and tag > 0 else None,
              "type": str(unit.get("type", "unknown"))[:48]}
    for key in ("x", "y", "health", "health_max", "shield"):
        value = unit.get(key)
        result[key] = value if _finite(value) and (key in ("x", "y") or value >= 0) else None
    return result


def _target_order(target: dict) -> tuple:
    return (target["tag"] is None, target["tag"] or 0, target["type"],
            *(target[key] if target[key] is not None else math.inf
              for key in ("x", "y", "health", "health_max", "shield")))


def combat_context(observation: dict) -> dict:
    """Current public force observations and native cumulative score, never inferred losses/actions.

    Targets contain at most eight structures and eight units, with explicit omitted
    and unknown-visibility counts. Health sums cover only valid paired health/max
    values across every ready owned combat unit in the supplied public units list;
    they do not estimate shields, force readiness, safety, or a recommended action.
    """
    score = observation.get("score")
    score = score if isinstance(score, dict) else {}
    totals = {}
    for key in ("lost_minerals", "lost_vespene", "killed_minerals", "killed_vespene"):
        group = score.get(key)
        value = group.get("army") if isinstance(group, dict) else None
        totals[key] = value if _finite(value) and value >= 0 else None
    raw_units = observation.get("units")
    enemies, damaged, orders, positioning = None, None, None, None
    enemy_targets, army_health = None, None
    if isinstance(raw_units, list):
        enemies, damaged = Counter(), []
        army_units = []
        targets = {"structures": [], "units": []}
        unknown_visibility, unclassified = 0, 0
        native_visible_only = observation.get("engine") == "sc2"
        player_id = observation.get("player_id", 1)
        for unit in raw_units:
            if not isinstance(unit, dict):
                continue
            name = str(unit.get("type", "unknown"))[:48]
            if unit.get("owner") == "enemy":
                visible = _enemy_visible(unit, native_visible_only=native_visible_only)
                if visible is None:
                    unknown_visibility += 1
                elif visible:
                    enemies[name] += 1
                    if isinstance(unit.get("is_structure"), bool):
                        targets["structures" if unit["is_structure"] else "units"].append(_enemy_target(unit))
                    else:
                        unclassified += 1
            if (
                unit.get("owner") not in ("self", player_id)
                or unit.get("is_structure") is not False
                or unit.get("is_ready") is not True
                or name.upper() in {"SCV", "MULE", "PROBE", "DRONE"}
            ):
                continue
            army_units.append(unit)
            hp, maximum = unit.get("health"), unit.get("health_max")
            if not (_finite(hp) and _finite(maximum) and 0 <= hp < maximum):
                continue
            item = {"type": name, "health": hp, "health_max": maximum, "health_ratio": round(hp / maximum, 4)}
            for key in ("tag", "x", "y", "shield"):
                if _finite(unit.get(key)):
                    item[key] = unit[key]
            damaged.append(item)
        enemies = dict(enemies.most_common(32))
        damaged = sorted(damaged, key=lambda item: item["health_ratio"])[:6]
        counts = {key: len(values) for key, values in targets.items()}
        enemy_targets = {
            **{key: sorted(values, key=_target_order)[:8] for key, values in targets.items()},
            "counts": {**counts, "unclassified": unclassified},
            "omitted": {**{key: max(0, count - 8) for key, count in counts.items()},
                        "unclassified": unclassified},
            "visibility_unknown": unknown_visibility,
            "visibility_complete": unknown_visibility == 0,
            "visibility_basis": "native_public_visible_only_or_explicit_flags" if native_visible_only
            else "explicit_visibility_flags_only",
            "scope": "current_visible_supplied_enemies_not_global_map_visibility",
        }
        health_pairs = [(unit.get("health"), unit.get("health_max")) for unit in army_units
                        if _finite(unit.get("health")) and _finite(unit.get("health_max"))
                        and 0 <= unit["health"] <= unit["health_max"] and unit["health_max"] > 0]
        health_sum = sum(hp for hp, _maximum in health_pairs) if health_pairs or not army_units else None
        max_sum = sum(maximum for _hp, maximum in health_pairs) if health_pairs or not army_units else None
        army_health = {
            "ready_units": len(army_units), "known_units": len(health_pairs),
            "unknown_units": len(army_units) - len(health_pairs),
            "full_health_units": sum(hp == maximum for hp, maximum in health_pairs),
            "damaged_units": sum(hp < maximum for hp, maximum in health_pairs),
            "health_sum": health_sum if _finite(health_sum) else None,
            "health_max_sum": max_sum if _finite(max_sum) else None,
            "coverage": round(len(health_pairs) / len(army_units), 4) if army_units else None,
            "coverage_complete": len(health_pairs) == len(army_units),
            "scope": "all_ready_owned_nonworker_nonstructures_in_supplied_units_health_only",
        }
        # Summarize actual orders and separation, without inferring that a force
        # is assembled, safe or strong enough. Missing positions/orders stay unknown.
        order_counts, unknown_orders = Counter(), 0
        points = []
        for unit in army_units:
            raw_orders = unit.get("orders")
            if not isinstance(raw_orders, list):
                unknown_orders += 1
            elif not raw_orders:
                order_counts["NO_CURRENT_ORDER"] += 1
            else:
                first = raw_orders[0]
                name = first.get("ability") if isinstance(first, dict) else None
                if isinstance(name, str) and name:
                    order_counts[name[:64]] += 1
                else:
                    unknown_orders += 1
            if all(_finite(unit.get(key)) for key in ("x", "y")):
                points.append((unit["x"], unit["y"]))
        orders = {"primary_order_counts": dict(order_counts), "unknown_order_units": unknown_orders}
        complete = len(points) == len(army_units)
        distance = None
        if points and complete and len(points) <= 512:
            distance = round(max((math.dist(a, b) for index, a in enumerate(points)
                                  for b in points[index + 1:]), default=0.0), 3)
        positioning = {"ready_units": len(army_units), "positioned_units": len(points),
                       "positions_complete": complete, "max_pair_distance": distance,
                       "distance_kind": "euclidean_map_units_not_path_or_combat_range"}
    army = observation.get("army_count")
    return {
        "army_ready": army if isinstance(army, int) and not isinstance(army, bool) and army >= 0 else None,
        "ready_counts": _counts(observation["ready_counts"])
        if isinstance(observation.get("ready_counts"), dict)
        else None,
        "queued_counts": _counts(observation["queued_counts"])
        if isinstance(observation.get("queued_counts"), dict)
        else None,
        "visible_enemy_counts": enemies,
        "visible_enemy_targets": enemy_targets,
        "most_damaged_army": damaged,
        "army_health": army_health,
        "army_orders": orders,
        "army_positioning": positioning,
        "army_score_kind": "cumulative-provider-score-not-a-recent-delta",
        "score_source": score["source"][:64] if isinstance(score.get("source"), str) else None,
        "army_score_totals": totals,
    }


def action_context(role: str, observation: dict) -> dict:
    """Project current native eligibility and observed workload for one role.

    A provided action_state is authoritative and missing entries fail closed. Without
    that optional field, older adapters retain the static role tools and are marked
    unchecked; this must not be described as native availability validation.
    No action is selected here and no scenario-specific build order is encoded.
    """
    if role not in ROLES:
        raise ValueError(f"Unknown agent role: {role!r}")
    tools = ROLE_TOOLS[role]
    raw = observation.get("action_state")
    provided = observation.get("action_context")
    allowed, blocked, eligible = [], {}, {}
    gather_resources = None
    scout_state = None
    guided = observation.get("guidance_enabled", True) is not False
    if not guided:
        allowed, source, known = list(tools), "legacy-guidance-disabled", False
    elif "action_state" in observation:
        source = "native-action-state"
        raw = raw if isinstance(raw, dict) else {}
        known = all(
            isinstance(raw.get(tool), dict) and isinstance(raw[tool].get("available"), bool) for tool in tools
        )
        for tool in tools:
            state = raw.get(tool)
            if isinstance(state, dict) and state.get("available") is True:
                allowed.append(tool)
                eligible[tool] = _tags(state.get("unit_tags"))[:48]
                if tool == "gather" and isinstance(state.get("resources"), list):
                    gather_resources = [name for name in ("minerals", "gas") if name in state["resources"]]
                    if not gather_resources:
                        allowed.remove(tool)
                        eligible.pop(tool)
                        blocked[tool] = "No available gather resource was reported"
            else:
                reason = state.get("reason") if isinstance(state, dict) else None
                blocked[tool] = str(reason or "Availability was not reported for the current observation")[
                    :240
                ]
    elif isinstance(provided, dict) and isinstance(provided.get("allowed_tools"), list):
        source, known = "provided-action-context", provided.get("availability_known", True) is True
        allowed = [tool for tool in tools if tool in provided["allowed_tools"]]
        reasons = provided.get("blocked_tools", {})
        reasons = reasons if isinstance(reasons, dict) else {}
        for tool in tools:
            if tool not in allowed:
                blocked[tool] = str(reasons.get(tool, "Not available in current action context"))[:240]
        if "gather" in allowed and isinstance(provided.get("gather_resources"), list):
            gather_resources = [name for name in ("minerals", "gas") if name in provided["gather_resources"]]
            if not gather_resources:
                allowed.remove("gather")
                blocked["gather"] = "No available gather resource was reported"
    else:
        allowed, source, known = list(tools), "legacy-static-unchecked", False
    if guided and role == "tactics":
        scout = raw.get("scout") if isinstance(raw, dict) else None
        if scout is None and isinstance(provided, dict):
            scout = provided.get("scout_state")
        if isinstance(scout, dict):
            tags = _tags(scout["unit_tags"]) if isinstance(scout.get("unit_tags"), list) else None
            default_tag = scout.get("default_unit_tag")
            if not isinstance(default_tag, int) or isinstance(default_tag, bool) or default_tag <= 0:
                default_tag = None
            selected = tags[:48] if tags is not None else None
            if tags is not None and default_tag in tags and default_tag not in selected:
                selected[-1:] = [default_tag]
            if "scout" in eligible and selected is not None:
                eligible["scout"] = selected
            default_available = scout.get("default_available")
            scout_state = {
                "unit_tags": selected,
                "default_unit_tag": default_tag,
                "default_available": default_available if isinstance(default_available, bool) else None,
            }
            if default_available is False and not selected and "scout" in allowed:
                allowed.remove("scout")
                eligible.pop("scout", None)
                blocked["scout"] = "Default scout unavailable and no explicit eligible scout was reported"
    worker_state = observation.get("worker_state", {})
    worker_state = worker_state if isinstance(worker_state, dict) else {}
    workload = {"workers": observation.get("workers")}
    for label, key in (
        ("idle", "idle_tags"),
        ("harvesting", "harvesting_tags"),
        ("constructing", "constructing_tags"),
        ("moving", "moving_tags"),
        ("eligible_builders", "eligible_builder_tags"),
    ):
        workload[label] = len(_tags(worker_state[key])) if key in worker_state else None
    if (
        isinstance(worker_state.get("eligible_builder_count"), int)
        and not isinstance(worker_state["eligible_builder_count"], bool)
        and worker_state["eligible_builder_count"] >= 0
    ):
        workload["eligible_builders"] = worker_state["eligible_builder_count"]
    # Capacity is only reported when observed; worker count alone is not saturation.
    for key in ("mineral_workers", "gas_workers", "mineral_capacity", "gas_capacity"):
        if key in worker_state:
            workload[key] = deepcopy(worker_state[key])
    for key in ("mineral_worker_count", "gas_worker_count", "unknown_harvest_worker_count"):
        if key in worker_state:
            workload[key] = deepcopy(worker_state[key])
    receipts = observation.get("last_receipts", [])
    # Share only reported native blockers. A generic missing-ability reason does
    # not establish which prerequisite failed; the catalog supplies static facts.
    team_blockers = {}
    if guided and isinstance(raw, dict):
        for owner, owner_tools in ROLE_TOOLS.items():
            reported = {
                tool: str(raw[tool].get("reason") or "Unavailable in current native observation")[:240]
                for tool in owner_tools
                if isinstance(raw.get(tool), dict) and raw[tool].get("available") is False
            }
            if reported:
                team_blockers[owner] = reported
    corrections = (
        [
            {
                "tool": str(receipt.get("tool", ""))[:48],
                "reason": str(receipt.get("reason", "Unspecified rejection"))[:240],
                "command_id": str(receipt.get("command_id", ""))[:64],
            }
            for receipt in receipts[-8:]
            if isinstance(receipt, dict)
            and receipt.get("actor_id", role) == role
            and receipt.get("status") == "rejected"
        ][-4:]
        if isinstance(receipts, list)
        else []
    )
    return {
        "source": source,
        "availability_known": known,
        "allowed_tools": allowed,
        "blocked_tools": blocked,
        "team_blockers": team_blockers,
        "eligible_unit_tags": eligible,
        "gather_resources": gather_resources,
        "scout_state": scout_state,
        "in_progress": {
            "structures": _counts(observation.get("pending_counts")),
            "units": _counts(observation.get("queued_counts")),
        },
        "worker_saturation": workload,
        "recent_rejections": corrections,
        "recent_execution_failures": recent_execution_failures(role, receipts),
    }

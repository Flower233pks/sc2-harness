"""Explicit reference-strategy scaffolds; never game actions or success criteria."""

from __future__ import annotations

import math
from copy import deepcopy

REFERENCE_PROFILE = "marine_reference_v1"
REFERENCE_LABEL = "给定 Marine 参考策略的 LLM 协同执行基线"
REFERENCE_REVISION = 3
_DEPOTS = ("SUPPLYDEPOT", "SUPPLYDEPOTLOWERED")
_OPENING_TARGETS = (("workers", "economy", "train_worker", 14, ("SCV",)),
                    ("ready_supply_depots", "economy", "build_supply", 1, _DEPOTS),
                    ("barracks", "production", "build_barracks", 1, ("BARRACKS",)))
_FORMATION_TARGETS = (("workers", "economy", "train_worker", 20, ("SCV",)),
                      ("barracks", "production", "build_barracks", 2, ("BARRACKS",)),
                      ("ready_marines", "production", "train_marine", 12, ("MARINE",)),
                      ("free_supply", "economy", "build_supply", 4, _DEPOTS))
_WORKERS = {"SCV", "MULE", "PROBE", "DRONE"}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _ready(observation: dict, name: str):
    counts = observation.get("ready_counts")
    return _count(counts.get(name, 0)) if isinstance(counts, dict) else None


def _counts(observation: dict, field: str, names: tuple[str, ...]):
    counts = observation.get(field)
    if not isinstance(counts, dict):
        return None
    values = [_count(counts.get(name, 0)) for name in names]
    return sum(values) if all(value is not None for value in values) else None


def _targets(definitions: tuple, observed: dict, observation: dict, worker_source: str | None) -> list[dict]:
    result = []
    for key, role, tool, target, types in definitions:
        value = observed[key]
        result.append({"id": key, "role": role, "tool": tool, "target": target, "observed": value,
                       "known": value is not None, "met": value >= target if value is not None else None,
                       "gap": max(0, target - value) if value is not None else None,
                       "progress": round(min(1, max(0, value / target)), 4) if value is not None else None,
                       "in_progress": {"types": list(types), "pending": _counts(observation, "pending_counts", types),
                                       "queued": _counts(observation, "queued_counts", types)}})
        if key == "workers":
            result[-1]["observed_source"] = worker_source
    return result


def _initial_hint_progress(observation: dict, previous: dict, now, phase: str) -> dict:
    """Historical advisory search evidence; never a selected destination or game action."""
    map_data = observation.get("map")
    point = map_data.get("enemy_start") if isinstance(map_data, dict) else None
    hint = list(point) if isinstance(point, (list, tuple)) and len(point) == 2 \
        and all(_number(value) is not None for value in point) else None
    expected = _count(observation.get("army_count"))
    player_id = observation.get("player_id", 1)
    positions = {}
    units = observation.get("units")
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict) or unit.get("owner") not in {"self", player_id} \
                or unit.get("is_structure") is not False or not isinstance(unit.get("type"), str) \
                or unit.get("type") in _WORKERS:
            continue
        if unit.get("is_ready") is not True:
            continue
        tag = _count(unit.get("tag"))
        x, y = _number(unit.get("x")), _number(unit.get("y"))
        if tag and x is not None and y is not None:
            positions.setdefault(tag, (x, y))
    known = len(positions)
    complete = isinstance(units, list) and expected is not None and known == expected
    near = sum(math.dist(position, hint) <= 15 for position in positions.values()) if hint is not None else None
    majority = near >= 2 and near > expected / 2 if complete and hint is not None else None
    prior = previous.get("initial_hint_public_progress")
    prior = prior if isinstance(prior, dict) and prior.get("hint_position") == hint \
        and previous.get("phase") == "recommended_assault_phase" else {}
    launched_reference = phase == "recommended_assault_phase"
    reached = launched_reference and (prior.get("initial_hint_reached") is True or majority is True)
    return {"hint_position": hint, "radius": 15, "army_ready_total": expected,
            "known_total": known, "near_count": near,
            "coverage": round(known / expected, 4) if expected and known <= expected else None,
            "positions_complete": complete, "majority_near": majority,
            "initial_hint_reached": reached,
            "reached_at_game_time": prior.get("reached_at_game_time")
            if prior.get("initial_hint_reached") is True else now if reached else None,
            "observed_at_game_time": now, "scope": "advisory_public_main_army_search_not_formal_goal"}


def _continuous_duties(observation: dict, observed: dict, hint: dict, phase: str) -> dict:
    state = observation.get("action_state")
    train = state.get("train_marine") if isinstance(state, dict) else None
    train = train if isinstance(train, dict) else {}
    available = train.get("available") if isinstance(train.get("available"), bool) else None
    supply_blocked = True if available is False and train.get("reason") == "insufficient_supply" else \
        False if available is True else None
    production = observation.get("production")
    ready_barracks = [unit for unit in production if isinstance(unit, dict) and unit.get("type") == "BARRACKS"
                      and unit.get("is_ready") is True] if isinstance(production, list) else []
    idle = sum(unit["is_busy"] is False for unit in ready_barracks) \
        if isinstance(production, list) and observed["barracks"] == len(ready_barracks) \
        and all(isinstance(unit.get("is_busy"), bool) for unit in ready_barracks) else None
    progress = {
        "economy": {"workers": observed["workers"], "free_supply": observed["free_supply"],
                    "marine_supply_blocked": supply_blocked},
        "production": {"ready_marines": observed["ready_marines"],
                       "queued_marines": _counts(observation, "queued_counts", ("MARINE",)),
                       "idle_ready_barracks": idle, "train_marine_available": available},
        "tactics": {"initial_hint_reached": hint["initial_hint_reached"]},
    }
    activities = {"economy": "support_marine_income_and_supply",
                  "production": "train_affordable_marines_from_idle_barracks",
                  "tactics": "advance_main_army_and_engage_current_targets"
                  if phase == "recommended_assault_phase" else "reconnaissance_defense_and_combat"}
    return {role: {"owner": role, "activity": activity, "continues_after_setup": True,
                   "until": "native_terminal", "progress": progress[role]}
            for role, activity in activities.items()}


def reference_plan(profile: str, observation: dict, previous: dict | None = None) -> dict | None:
    """Advance an advisory phase only from committed observations, never proposals.

    Reaching the reference unit count latches a recommended assault phase. It
    does not prove assembly, an issued attack, combat readiness, or a native win.
    """
    if profile == "none":
        return None
    if profile != REFERENCE_PROFILE:
        raise ValueError(f"Unknown guidance profile: {profile}")
    previous = previous if isinstance(previous, dict) and previous.get("profile") == profile \
        and previous.get("version") == 1 and previous.get("revision") == REFERENCE_REVISION \
        and previous.get("advisory") is True else {}
    now = _number(observation.get("game_time"))
    workers = _count(observation.get("workers"))
    worker_source = "observation.workers" if workers is not None else None
    if workers is None:
        workers = _ready(observation, "SCV")
        worker_source = "ready_counts.SCV" if workers is not None else None
    barracks = _ready(observation, "BARRACKS")
    if barracks is None and isinstance(observation.get("structures"), dict):
        barracks = _count(observation["structures"].get("BARRACKS", 0))
    depots = _counts(observation, "ready_counts", _DEPOTS)
    if depots is None:
        depots = _counts(observation, "structures", _DEPOTS)
    marines = _ready(observation, "MARINE")
    cap, used = _number(observation.get("supply_cap")), _number(observation.get("supply_used"))
    free_supply = cap - used if cap is not None and used is not None and cap >= 0 and used >= 0 else None
    observed = {"workers": workers, "ready_supply_depots": depots, "barracks": barracks,
                "ready_marines": marines, "free_supply": free_supply}
    opening_targets = _targets(_OPENING_TARGETS, observed, observation, worker_source)
    reached = marines is not None and marines >= 12
    prior_assault = previous.get("phase") == "recommended_assault_phase"
    opening_observed = all(target["met"] is True for target in opening_targets)
    prior_formation = previous.get("phase") == "form_reference_force"
    phase = "recommended_assault_phase" if reached or prior_assault else \
        "form_reference_force" if opening_observed or prior_formation else "reference_opening"
    soft_targets = opening_targets if phase == "reference_opening" else \
        _targets(_FORMATION_TARGETS, observed, observation, worker_source)
    entered = previous.get("phase_entered_game_time") if previous.get("phase") == phase else now
    reached_at = previous.get("reference_size_reached_at_game_time") if prior_assault else now if reached else None
    hint_progress = _initial_hint_progress(observation, previous, now, phase)
    return {"profile": profile, "version": 1, "revision": REFERENCE_REVISION,
            "label": REFERENCE_LABEL, "advisory": True,
            "phase": phase, "phase_entered_game_time": entered,
            "reference_size_reached_at_game_time": reached_at, "observed_at_game_time": now,
            "soft_targets": soft_targets,
            "continuous_duties": _continuous_duties(observation, observed, hint_progress, phase),
            "initial_hint_public_progress": hint_progress,
            "scope": "reference_only_not_success_or_issued_actions"}


def role_reference_plan(role: str, plan: dict, observation: dict) -> dict:
    """Attach a public execution assignment without choosing or constraining commands."""
    focus = {
        "economy": (
            "Own mineral income, about 20 workers and 4 free population supply. Production owns Barracks/"
            "Marines. Resolve actual Marine supply blockers; check ready/pending Depots before purchases. "
            "Existing jobs are paid. Marines cost 50 minerals/zero gas; gas is for explicit new research. "
            "A second base is optional. Coordinate shared minerals; choose useful commands or actions=[]."
        ),
        "production": (
            "About 2 ready Barracks/12 Marines are launch references, never a production quota or cap. "
            "Keep training affordable Marines from idle Barracks until native Victory even after setup "
            "targets are met. Economy owns workers/Depots: coordinate minerals/supply. Pending jobs are "
            "paid, not new deductions or Barracks blockers. Check queues; extra infrastructure needs an "
            "observed capacity reason."
        ),
        "tactics": (
            "You own reconnaissance, defense and combat, not workers, Depots or Barracks. Production owns "
            "the reference Marine count; do not treat another role's setup gaps as your own actions. "
            "Coordinate the observed force. Choose a rendezvous "
            "and an observable condition for starting or ending a wait; a count alone does not prove units "
            "are together. Existing forces may act before the reference size if justified. A continuously "
            "producing queue and absence of fresh enemy vision are not permanent waiting conditions. "
            "Select your own legal movement, scouting, combat or wait commands."
        ),
    }
    if role not in focus:
        raise ValueError(f"Unknown agent role: {role}")
    if plan.get("phase") == "reference_opening":
        focus.update({
            "economy": (
                "Your opening reference targets are about 14 workers and 1 ready Supply Depot. The first "
                "ready Depot unlocks production's first Barracks. Check ready and pending Depots before "
                "starting another; a funded in-progress building is not a missing purchase. Maintain "
                "mineral income and coordinate the shared minerals needed for the first Barracks. If your "
                "own targets are met or in progress, actions=[] can leave minerals for production rather "
                "than piling up unrelated Depots. Barracks belong to production. A second base is optional, "
                "not an assigned prerequisite; gas is for explicit new research spending, not spare capacity."
            ),
            "production": (
                "Your opening reference target is 1 ready Barracks. A completed Supply Depot is an economy "
                "prerequisite: ask its owner if missing, do not take over its target or substitute an "
                "Engineering Bay. Check pending Barracks before repeating construction. Coordinate the "
                "shared mineral budget with economy. Pending jobs are already paid, not another deduction "
                "from the current minerals; a second Barracks or research is not needed to start "
                "Marines. Once a Barracks can train affordable Marines, you may do so without waiting for "
                "every other setup reference target. Choose useful own-role commands or wait for an "
                "observed blocker; reference phases never mask otherwise legal tools."
            ),
            "tactics": (
                "You have no numeric opening setup targets. Workers, the first Depot and the first "
                "Barracks belong to economy and production; communicate observed blockers without "
                "adopting their jobs. There is no mandatory opening scout: do not send an SCV toward "
                "the enemy merely to satisfy this phase. With no combat force or observed threat, "
                "actions=[] is valid while teammates establish production. You still choose any useful "
                "legal scouting, defense or combat action; setup references are not action gates."
            ),
        })
    if plan.get("phase") == "recommended_assault_phase" and role == "tactics":
        hint_reached = plan.get("initial_hint_public_progress", {}).get("initial_hint_reached") is True
        focus[role] = (
            "Own combat, defense and reconnaissance. The launch reference was observed, not an issued "
            "assault. Prioritize currently visible living enemy structures at their own observed coordinates; "
            "keep each target's health and position together. enemy_start is an initial search hint, not a "
            "live entity. Empty vision does not prove the map clear. "
            + (
                "Public observations show the main army previously reached that initial hint. Without "
                "native Victory and with no live target, continue MAIN-ARMY search of other known resource "
                "clusters or public locations; one scout cannot replace advancing that army. You choose "
                "destinations and legal commands. Last-seen positions are historical, not current facts. "
                if hint_reached else
                "The main army has not been confirmed at that initial hint. Without a visible building "
                "target, bring the main army toward the known initial hint using your chosen attack-move "
                "or other justified legal command. One scout is not the main army. Do not replace this "
                "initial main-army search with endless expansion scouting while the army stays home. "
            )
            + "Inspect current positions, health and threats; choose commands and justified revisions yourself. "
            "Reference progress is not a formal goal, proof of clearing the hint, or an action gate."
        )
    state = observation.get("action_state")
    supply_blocked = isinstance(state, dict) and isinstance(state.get("train_marine"), dict) \
        and state["train_marine"].get("available") is False \
        and state["train_marine"].get("reason") == "insufficient_supply"
    if role == "economy" and supply_blocked:
        focus[role] = "Current native Marine production reports insufficient_supply; resolving this is your immediate team blocker. " + focus[role]
    result = deepcopy(plan)
    result.pop("soft_targets", None)
    duties = result.pop("continuous_duties", {})
    if isinstance(duties, dict) and role in duties:
        result["continuous_duty"] = duties[role]
    if role != "tactics":
        result.pop("initial_hint_public_progress", None)
    result["role"] = role
    result["role_targets"] = [deepcopy(target) for target in plan.get("soft_targets", [])
                              if target.get("role") == role]
    result["role_progress"] = {
        "assigned_targets": len(result["role_targets"]),
        "met_targets": sum(target.get("met") is True for target in result["role_targets"]),
        "unmet_targets": sum(target.get("met") is False for target in result["role_targets"]),
        "unknown_targets": sum(target.get("met") is None for target in result["role_targets"]),
        "observed_at_game_time": plan.get("observed_at_game_time"),
    }
    result["focus"] = focus[role]
    return result

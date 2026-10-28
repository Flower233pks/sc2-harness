"""Mission results derived from committed game observations, never model claims."""
from __future__ import annotations

import math
from numbers import Real

from .missions import get_mission


def _number(value) -> int | float | None:
    if not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return None
    return value


def _counts(source: object, names: list[str]) -> int | float | None:
    if not isinstance(source, dict):
        return None
    values = [_number(source.get(name, 0)) for name in names]
    return sum(values) if all(value is not None for value in values) else None


def _expanded_bases(observation: dict, criterion: dict) -> int | None:
    units = observation.get("units")
    if not isinstance(units, list):
        return None
    positions = []
    for unit in units:
        if (not isinstance(unit, dict) or unit.get("owner") != "self"
                or unit.get("type") not in criterion["types"] or unit.get("is_ready") is not True):
            continue
        x, y = _number(unit.get("x")), _number(unit.get("y"))
        if x is None or y is None:
            return None
        positions.append((x, y))
    # Capped at two distinct bases: duplicate CCs within one location do not establish an expansion.
    if any(math.dist(a, b) >= criterion["min_distance"] for index, a in enumerate(positions)
           for b in positions[index + 1:]):
        return 2
    return min(1, len(positions))


def _known_expansion_sites(observation: dict, criterion: dict) -> int | None:
    world = observation.get("map")
    if not isinstance(world, dict):
        return None
    start, sites = world.get("start"), world.get("expansions")
    if not isinstance(start, (list, tuple)) or len(start) != 2 or not isinstance(sites, list):
        return None
    origin = tuple(_number(value) for value in start)
    if any(value is None for value in origin):
        return None
    discovered, unknown = set(), False
    for site in sites:
        if not isinstance(site, (list, tuple)) or len(site) != 2:
            unknown = True
            continue
        point = tuple(_number(value) for value in site)
        if any(value is None for value in point):
            unknown = True
            continue
        if math.dist(origin, point) > criterion["min_distance"]:
            discovered.add(point)
    # A valid positive observation proves existence even if an unrelated entry is malformed.
    return len(discovered) if discovered or not unknown else None


def _observed_value(observation: dict, criterion: dict) -> int | float | None:
    metric = criterion["metric"]
    ready = observation.get("ready_counts")
    if metric == "workers":
        return _counts(ready, ["SCV"]) if isinstance(ready, dict) else _number(observation.get("workers"))
    if metric == "supply_cap":
        return _number(observation.get("supply_cap"))
    if metric == "structures":
        return _counts(ready if isinstance(ready, dict) else observation.get("structures"), criterion["types"])
    if metric == "units":
        if criterion.get("requires_upgrade"):
            upgrades = observation.get("upgrades")
            completed = upgrades.get("completed") if isinstance(upgrades, dict) else None
            if not isinstance(completed, list):
                return None
            if criterion["requires_upgrade"] not in completed:
                return 0
        return _counts(ready if isinstance(ready, dict) else observation.get("unit_counts"), criterion["types"])
    if metric == "expanded_bases":
        return _expanded_bases(observation, criterion)
    if metric == "known_expansion_site":
        return _known_expansion_sites(observation, criterion)
    if metric == "collected_vespene":
        score = observation.get("score")
        return _number(score.get("collected_vespene")) if isinstance(score, dict) else None
    if metric == "upgrade":
        upgrades = observation.get("upgrades")
        completed = upgrades.get("completed") if isinstance(upgrades, dict) else None
        return int(criterion["upgrade"] in completed) if isinstance(completed, list) else None
    if metric == "scouted":
        value = observation.get("scouted")
        return int(value) if isinstance(value, bool) else None
    if metric == "native_victory":
        if observation.get("engine") not in {None, "sc2"}:
            return 0
        return int(observation["result"] == "Victory") if "result" in observation else None
    raise ValueError(f"unknown observation metric: {metric}")


def _progress(items: list[dict]) -> float:
    if not items:
        return 0
    values = [1 if item["achieved"] else min(1, item["value"] / item["target"])
              if item["known"] and item["eligible"] else 0 for item in items]
    return round(sum(values) / len(items), 4)


def _evaluate(scenario: str, observation: dict, previous: list[dict] | dict | None) -> tuple[list[dict], list[dict]]:
    definition = get_mission(scenario)
    if isinstance(previous, dict):
        previous = previous.get("milestones", []) if previous.get("scenario") == scenario else []
    old = {item["id"]: item for item in previous or []}
    results, stages, completed = [], [], set()
    now = _number(observation.get("game_time"))
    for stage in definition["stages"]:
        eligible = all(key in completed for key in stage["prerequisites"])
        items = []
        for criterion in stage["criteria"]:
            was = old.get(criterion["id"], {})
            value = _observed_value(observation, criterion)
            achieved = eligible and (bool(was.get("achieved")) or (value is not None and value >= criterion["target"]))
            item = {
                **criterion, "stage_id": stage["id"], "value": value, "known": value is not None,
                "eligible": eligible, "achieved": achieved, "reported": was.get("reported", False),
                "achieved_at_game_time": was.get("achieved_at_game_time") if was.get("achieved") and achieved
                else now if achieved else None,
            }
            items.append(item)
        done = bool(items) and all(item["achieved"] for item in items)
        if done:
            completed.add(stage["id"])
        times = [item["achieved_at_game_time"] for item in items]
        completed_at = max(times) if done and all(t is not None for t in times) else None
        stages.append({
            "id": stage["id"], "label": stage["label"], "policy_goal": stage["policy_goal"],
            "prerequisites": stage["prerequisites"], "criteria": items,
            "status": "complete" if done else "active" if eligible else "blocked", "complete": done,
            "progress": _progress(items), "achieved_at_game_time": completed_at,
        })
        results.extend(items)
    return results, stages


def milestones(observation: dict, previous: list[dict] | None = None,
               scenario: str = "build_and_scout") -> list[dict]:
    """Compatible default mission; achieved thresholds latch, never sum units across observations."""
    return _evaluate(scenario, observation, previous)[0]


def outcome(scenario: str, observation: dict, achieved: list[dict]) -> str | None:
    definition = get_mission(scenario)
    native = observation.get("result")
    if native == "Defeat":
        return "failure"
    if scenario == "full_match":
        if native == "Victory" and observation.get("engine") not in {None, "sc2"}:
            return "incomplete"
        return "success" if native == "Victory" else "incomplete" if native == "Tie" else None
    expected = {criterion["id"] for stage in definition["stages"] for criterion in stage["criteria"]}
    actual = {item["id"] for item in achieved if item.get("achieved")}
    if expected <= actual:
        return "success"
    if native in {"Victory", "Tie"}:
        return "incomplete"
    return None


def evaluate_mission(scenario: str, observation: dict, previous: list[dict] | dict | None = None, *,
                     terminated: bool = False, termination_reason: str | None = None) -> dict:
    """One observation update. Budget/stop censor unfinished goals; native defeat remains failure."""
    items, stages = _evaluate(scenario, observation, previous)
    result = outcome(scenario, observation, items)
    terminal = terminated or observation.get("result") in {"Victory", "Defeat", "Tie"}
    censored = bool(terminal and result != "success" and observation.get("result") != "Defeat")
    for item in items:
        item["censored"] = bool(censored and not item["achieved"])
    for stage in stages:
        stage["censored"] = bool(censored and not stage["complete"])
    current = next((stage for stage in stages if stage["status"] == "active"), None)
    return {
        "scenario": scenario, "mission_version": get_mission(scenario)["version"],
        "milestones": items, "stages": stages, "current_stage": current,
        "stage_completion": {"completed": sum(stage["complete"] for stage in stages), "total": len(stages)},
        "progress": _progress(items), "complete": all(stage["complete"] for stage in stages),
        "outcome": result if result is not None else "incomplete" if terminal else None,
        "engine_result": observation.get("result"), "censored": censored,
        "termination_reason": termination_reason, "observed_at_game_time": _number(observation.get("game_time")),
    }


def quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    index = (len(data) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(data) - 1)
    return round(data[lower] + (data[upper] - data[lower]) * (index - lower), 2)

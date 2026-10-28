"""Declarative observation-based missions and role goals, without action recommendations."""
from __future__ import annotations

from copy import deepcopy

from .guidance import role_reference_plan


def _criterion(key: str, label: str, role: str, metric: str, target: int, **extra) -> dict:
    return {"id": key, "label": label, "role": role, "metric": metric, "target": target, **extra}


def _opening() -> dict:
    return {
        "id": "opening", "label": "建立开局产能", "prerequisites": [],
        "policy_goal": "Establish the opening economy and completed supply and production infrastructure.",
        "criteria": [
            _criterion("workers", "同时拥有 14 名工人", "economy", "workers", 14),
            _criterion("supply", "完成 1 座补给站", "economy", "structures", 1,
                       types=["SUPPLYDEPOT", "SUPPLYDEPOTLOWERED"]),
            _criterion("barracks", "完成 1 座兵营", "production", "structures", 1, types=["BARRACKS"]),
        ],
    }


def _expansion() -> dict:
    return {
        "id": "expansion", "label": "扩张并增加劳动力", "prerequisites": ["opening"],
        "policy_goal": "Explore new resource locations, not only the enemy start: observe at least one resource "
                       "cluster more than 16 map units from the original starting position. "
                       "Reach 24 simultaneous workers and two completed bases at least 12 map units apart.",
        "criteria": [
            _criterion("known_expansion_site", "发现距开局基地超过 16 的资源簇", "tactics",
                       "known_expansion_site", 1, min_distance=16),
            _criterion("workers_24", "同时拥有 24 名工人", "economy", "workers", 24),
            _criterion("expanded_bases", "完成 2 个相距至少 12 的基地", "economy", "expanded_bases", 2,
                       types=["COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS"], min_distance=12),
        ],
    }


_BUILD = _opening()
_BUILD.update(id="build_and_scout", label="建造与侦察", policy_goal=(
    "Reach 14 workers, one completed supply depot, one completed barracks, four simultaneous Marines, "
    "and an observed scout arrival at the enemy starting area."
))
_BUILD["criteria"].extend([
    _criterion("marines", "同时拥有 4 名陆战队员", "production", "units", 4, types=["MARINE"]),
    _criterion("scout", "侦察单位实际到达敌方出生区域", "tactics", "scouted", 1),
])

_MISSIONS = {
    "build_and_scout": {
        "id": "build_and_scout", "name": "建造与侦察", "version": 1,
        "objective": _BUILD["policy_goal"], "stages": [_BUILD],
        "suggested_map": "Simple64",
        "suggested_budget": {"max_rounds": 60, "max_game_seconds": 300, "advance_loops": 112},
    },
    "full_match": {
        "id": "full_match", "name": "完整对局", "version": 1,
        "objective": "Win the native SC2 game; only an observed engine Victory counts.",
        "suggested_map": "Simple64",
        "stages": [{
            "id": "victory", "label": "赢下原生对局", "prerequisites": [],
            "policy_goal": "Win the native SC2 game using the available shared economy, production and army.",
            "criteria": [_criterion("native_victory", "原生引擎报告 Victory", "all", "native_victory", 1)],
        }],
        "suggested_budget": {"max_rounds": 300, "max_game_seconds": 2400, "advance_loops": 224},
    },
    "expansion_logistics": {
        "id": "expansion_logistics", "name": "扩张与资源供给", "version": 2,
        "objective": "Establish opening infrastructure, expand the economy, then establish sustained gas income.",
        "suggested_map": "AbyssalReefLE",
        "stages": [_opening(), _expansion(), {
            "id": "gas_logistics", "label": "建立双气矿供给", "prerequisites": ["expansion"],
            "policy_goal": "Complete two refineries and reach 200 cumulative vespene collected in engine score.",
            "criteria": [
                _criterion("refineries_2", "同时完成 2 座精炼厂", "economy", "structures", 2,
                           types=["REFINERY"]),
                _criterion("gas_200", "累计采集 200 气体", "economy", "collected_vespene", 200),
            ],
        }],
        "suggested_budget": {"max_rounds": 180, "max_game_seconds": 900, "advance_loops": 224},
    },
    "army_readiness": {
        "id": "army_readiness", "name": "步兵产能与升级备战", "version": 2,
        "objective": "Establish opening infrastructure, build infantry capacity, then field an upgraded Marine force.",
        "suggested_map": "AbyssalReefLE",
        "stages": [_opening(), {
            "id": "infantry_infrastructure", "label": "建立步兵生产与研究条件", "prerequisites": ["opening"],
            "policy_goal": "Complete two barracks and an engineering bay; collect at least 100 cumulative vespene.",
            "criteria": [
                _criterion("barracks_2", "同时完成 2 座兵营", "production", "structures", 2, types=["BARRACKS"]),
                _criterion("engineering_bay", "完成 1 座工程站", "production", "structures", 1,
                           types=["ENGINEERINGBAY"]),
                _criterion("gas_100", "累计采集 100 气体", "economy", "collected_vespene", 100),
            ],
        }, {
            "id": "upgraded_army", "label": "形成升级步兵部队", "prerequisites": ["infantry_infrastructure"],
            "policy_goal": "Complete Terran infantry weapons level 1, then observe at least 12 simultaneous "
                           "ready Marines while that upgrade is completed. Provide at least 30 supply capacity.",
            "criteria": [
                _criterion("army_supply_capacity", "提供至少 30 人口上限", "economy", "supply_cap", 30,
                           maintain=True),
                _criterion("infantry_weapons_1", "完成步兵武器 1 级", "production", "upgrade", 1,
                           upgrade="TERRANINFANTRYWEAPONSLEVEL1"),
                _criterion("marines_12", "武器 1 完成后同时拥有 12 名陆战队员", "production", "units", 12,
                           types=["MARINE"], requires_upgrade="TERRANINFANTRYWEAPONSLEVEL1"),
            ],
        }],
        "suggested_budget": {"max_rounds": 240, "max_game_seconds": 1200, "advance_loops": 224},
    },
    "expansion_and_victory": {
        "id": "expansion_and_victory", "name": "扩张后赢下对局", "version": 2,
        "objective": "Establish opening infrastructure, expand to two bases with 24 workers, then win the native game.",
        "suggested_map": "AbyssalReefLE",
        "stages": [_opening(), _expansion(), {
            "id": "victory", "label": "扩张后赢下原生对局", "prerequisites": ["expansion"],
            "policy_goal": "Win the native SC2 game after the expansion stage; an engine Victory is required.",
            "criteria": [_criterion("native_victory", "原生引擎报告 Victory", "all", "native_victory", 1)],
        }],
        "suggested_budget": {"max_rounds": 360, "max_game_seconds": 2400, "advance_loops": 224},
    },
}

_ROLE_DUTIES = {
    "economy": "Own workers, mineral and gas income, supply, refineries, and distinct-base expansion. "
               "Production owns infantry production and research; tactics owns scouting and combat.",
    "production": "Own production infrastructure, engineering research, and infantry production. "
                  "Economy owns workers, income, supply, refineries and expansion; tactics owns combat.",
    "tactics": "Own scouting, defense, and combat with observed available units. "
               "Economy owns workers and income; production owns infantry production and research.",
}

_CONTINUOUS_DUTIES = {
    "economy": "Continue supporting the army with mineral income, available supply and replacement workers "
               "when needed. Historical worker or expansion milestones do not mean the current economy is healthy.",
    "production": "Continue forming and replacing a combat army using your observed production capacity and "
                  "shared resources. Completed barracks alone cannot defeat an opponent. Choose appropriate "
                  "production and upgrades; there is no prescribed army size or build order.",
    "tactics": "Continue reconnaissance, defense and combat toward defeating the opponent. If no combat units "
               "exist, tell production that an army is missing; a stage named victory is not an observed win.",
}


def get_mission(scenario: str) -> dict:
    """Return a detached JSON-compatible definition; unknown names are configuration errors."""
    try:
        return deepcopy(_MISSIONS[scenario])
    except KeyError as exc:
        raise ValueError(f"unknown mission: {scenario}") from exc


def mission_catalog() -> list[dict]:
    return [get_mission(name) for name in _MISSIONS]


def get_role_task(role: str, evaluation: dict, observation: dict | None = None, *,
                  reference_plan: dict | None = None) -> dict:
    """Project current observed goals for a role, never choose its actions or claim future completion."""
    if role not in {"economy", "production", "tactics"}:
        raise ValueError(f"unknown role: {role}")
    mission = get_mission(evaluation["scenario"])
    stage = evaluation.get("current_stage")
    items = [
        item for item in evaluation.get("milestones", [])
        if stage and item.get("stage_id") == stage["id"] and item.get("role") in {role, "all"}
    ]
    role_milestones = [
        {"id": item["id"], "label": item["label"], "metric": item["metric"],
         "observed": item["value"], "target": item["target"], "known": item["known"],
         "done": bool(item["known"] and item["value"] >= item["target"])
         if item.get("maintain") else item["achieved"],
         "achieved_at_game_time": item["achieved_at_game_time"],
         **{key: item[key] for key in ("types", "upgrade", "requires_upgrade", "min_distance", "maintain")
            if key in item}}
        for item in items
    ]
    gaps = [
        {**item, "remaining": max(0, item["target"] - item["observed"]) if item["known"] else None}
        for item in role_milestones if not item["done"]
    ]
    targets = "; ".join(
        f"{item['metric']} ({','.join(item.get('types', [])) or item.get('upgrade', 'observed state')}): "
        f"requires_upgrade={item.get('requires_upgrade', 'none')}, "
        f"min_distance={item.get('min_distance', 'not applicable')}, "
        f"target={item['target']}, observed={item['observed']}, achieved={item['done']}"
        for item in role_milestones
    )
    discovery_goal = (
        "Discover actual resource clusters more than 16 map units from the original starting position; "
        "reaching the enemy start alone does not satisfy resource discovery. "
        if role == "tactics" and stage and stage["id"] == "expansion" else ""
    )
    role_objective = (
        f"You are the {role} role. {_ROLE_DUTIES[role]} "
        f"{discovery_goal}"
        f"Your goals in the current stage: {targets or 'no unmet owned milestone in this stage'}. "
        "Use only observed evidence. Completed milestones stay achieved; goals with maintain=true must also "
        "meet their target in the current observation. If your goals are done, actions=[] is valid; "
        "only maintain necessary support for the team rather than taking over another role."
    )
    continuous = any(item["metric"] == "native_victory" and not item["done"] for item in role_milestones)
    if continuous:
        continuous_duty = _CONTINUOUS_DUTIES[role]
        if mission["id"] == "full_match" and role == "production" and reference_plan is not None:
            continuous_duty = continuous_duty.replace(
                "there is no prescribed army size or build order.",
                "follow the given reference execution focus; its setup targets remain advisory, "
                "not native Victory requirements.",
            )
        role_objective += (
            " The native Victory goal is still pending. Entering this phase does not complete the task. "
            + continuous_duty
        )
    result = {
        "scenario": mission["id"], "role_objective": role_objective,
        "current_stage": stage, "role_phase": stage["id"] if stage else "complete",
        "role_done": not gaps, "role_milestones": role_milestones, "goal_gaps": gaps,
        "continuous_role_duty": continuous,
        "focus": stage["policy_goal"] if stage else "All observed mission stages are complete.",
        "observed_at_game_time": evaluation.get("observed_at_game_time"),
    }
    if mission["id"] == "full_match" and reference_plan is not None:
        result["reference_plan"] = role_reference_plan(role, reference_plan, observation or {})
    return result

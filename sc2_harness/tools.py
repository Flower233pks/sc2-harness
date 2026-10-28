"""Role authority, input validation and deterministic shared-budget arbitration."""
from __future__ import annotations

import math
from uuid import uuid4

TOOLS = {
    "train_worker": ("economy", 50, 0, "训练一名工人"),
    "gather": ("economy", 0, 0, "让闲置工人采集"),
    "build_supply": ("economy", 100, 0, "建造补给站"),
    "build_refinery": ("economy", 75, 0, "建造精炼厂"),
    "expand": ("economy", 400, 0, "建造新指挥中心"),
    "build_barracks": ("production", 150, 0, "建造兵营"),
    "build_engineering_bay": ("production", 125, 0, "建造工程站"),
    "train_marine": ("production", 50, 0, "训练陆战队员"),
    "research_infantry": ("production", 100, 100, "研究步兵武器等级 1"),
    "scout": ("tactics", 0, 0, "派出侦察单位"),
    "attack": ("tactics", 0, 0, "下达部队攻击命令"),
    "retreat": ("tactics", 0, 0, "撤退到指定位置"),
    "hold": ("tactics", 0, 0, "保持部队阵地"),
}


def catalog() -> list[dict]:
    return [{"id": key, "role": value[0], "minerals": value[1], "vespene": value[2],
             "description": value[3]} for key, value in TOOLS.items()]


def arbitrate(decisions: dict[str, dict], observation: dict, *,
              priority: tuple[str, ...] = ("economy", "production", "tactics")) -> tuple[list[dict], list[dict]]:
    """Reserve resource budgets per round, then defer actual feasibility to the game.

    The caller records priority. Default ordering preserves the original contract;
    the state-aware runtime rotates it so one role cannot always spend first.
    Do not call a rejected or accepted proposal 'task complete'.
    """
    accepted, receipts = [], []
    if len(priority) != 3 or set(priority) != {"economy", "production", "tactics"}:
        raise ValueError("priority must contain each role exactly once")
    minerals = float(observation.get("minerals", 0))
    vespene = float(observation.get("vespene", 0))
    units = {str(unit["tag"]): unit for unit in observation.get("units", [])}
    action_state = observation.get("action_state")
    for actor in priority:
        decision = decisions.get(actor, {})
        actions = decision.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        seen = set()
        for action in actions[:2]:
            command = {"command_id": uuid4().hex, "actor_id": actor,
                       "tool": action.get("tool", "") if isinstance(action, dict) else "",
                       "args": action.get("args", {}) if isinstance(action, dict) else {}}
            tool, args = command["tool"], command["args"]
            reason = None
            spec = TOOLS.get(tool) if isinstance(tool, str) else None
            if spec is None:
                reason = "unknown_tool"
            elif spec[0] != actor:
                reason = "role_not_authorized"
            elif tool in seen:
                reason = "duplicate_tool_in_round"
            elif not isinstance(args, dict) or set(args) - ({"resource", "max_workers"} if tool == "gather" else
                             {"x", "y", "unit_tag"} if tool == "scout" else
                             {"x", "y"} if tool in {"attack", "retreat", "build_supply", "build_refinery",
                                                    "expand", "build_barracks", "build_engineering_bay"} else set()):
                reason = "invalid_arguments"
            elif tool == "gather" and (args.get("resource", "minerals") not in ("minerals", "gas")
                    or not isinstance(args.get("max_workers", 3), int)
                    or isinstance(args.get("max_workers", 3), bool)
                    or not 1 <= args.get("max_workers", 3) <= 24):
                reason = "invalid_gather_arguments"
            elif ("x" in args) != ("y" in args):
                reason = "coordinates_require_x_and_y"
            elif "x" in args:
                x, y = args["x"], args["y"]
                width, height = observation.get("map", {}).get("width", 0), observation.get("map", {}).get("height", 0)
                if (not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                        or isinstance(x, bool) or isinstance(y, bool)
                        or not math.isfinite(x) or not math.isfinite(y)
                        or not (0 <= x <= width and 0 <= y <= height)):
                    reason = "destination_outside_map"
            if reason is None and "unit_tag" in args:
                unit = units.get(str(args["unit_tag"]))
                if unit is None or unit.get("owner") != "self":
                    reason = "unit_not_owned_or_not_visible"
                elif tool != "scout":
                    reason = "unit_tag_only_supported_for_scout"
            if reason is None and isinstance(action_state, dict):
                feasibility = action_state.get(tool)
                if isinstance(feasibility, dict) and feasibility.get("available") is False:
                    reason = "currently_unavailable:" + str(feasibility.get("reason", "engine_precondition"))
            if reason is None and (minerals < spec[1] or vespene < spec[2]):
                reason = "shared_budget_exhausted"
            if reason:
                receipts.append({**command, "status": "rejected", "reason": reason, "details": {"stage": "validation"}})
                continue
            seen.add(tool)
            minerals -= spec[1]
            vespene -= spec[2]
            accepted.append(command)
    return accepted, receipts

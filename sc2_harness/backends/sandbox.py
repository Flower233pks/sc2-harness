"""Deterministic protocol test adapter; NOT StarCraft II and never an automatic fallback."""
from __future__ import annotations

import copy
from pathlib import Path

from sc2_harness.tools import TOOLS


class SandboxBackend:
    @staticmethod
    def probe() -> dict:
        return {"id": "sandbox", "available": True, "label": "接口沙盒（非星际引擎）",
                "reason": "仅用于开发、故障测试及网页接口检查"}

    async def start(self, config: dict, run_dir: Path) -> dict:
        self.loop, self.pending, self.scouted, self.closed = 0, [], False, False
        self.minerals, self.vespene, self.supply_cap = 500.0, 0.0, 15
        self.counts = {"SCV": 12, "MARINE": 0}
        self.buildings = {"COMMANDCENTER": 1}
        self.next_tag = 1
        return self._result([])

    def _observation(self) -> dict:
        units = []
        for typ, amount in {**self.counts, **self.buildings}.items():
            for i in range(amount):
                index = len(units)
                units.append({"tag": str(index + 1), "type": typ, "x": 10 + index % 9,
                              "y": 10 + index // 9, "health": 45 if typ in self.counts else 400,
                              "health_max": 45 if typ in self.counts else 400, "shield": 0,
                              "owner": "self", "is_structure": typ in self.buildings,
                              "is_idle": True, "is_ready": True})
        if self.scouted:
            units.append({"tag": "9999", "type": "ZERGLING", "x": 49, "y": 47, "health": 35,
                          "health_max": 35, "shield": 0, "owner": "enemy", "is_structure": False,
                          "is_idle": False, "is_ready": True})
        return {"engine": "sandbox", "game_loop": self.loop, "game_time": self.loop / 22.4,
                "player_id": 1, "minerals": round(self.minerals), "vespene": round(self.vespene),
                "supply_used": sum(self.counts.values()), "supply_cap": self.supply_cap,
                "workers": self.counts["SCV"], "army_count": self.counts["MARINE"], "units": units,
                "structures": copy.deepcopy(self.buildings), "unit_counts": copy.deepcopy(self.counts),
                "pending": {key: sum(1 for p in self.pending if p[1] == key) for _, key, _ in self.pending},
                "map": {"width": 64, "height": 64, "start": [12, 12], "enemy_start": [50, 50],
                        "expansions": [[12, 28], [30, 12]]},
                "enemy_visible": int(self.scouted), "scouted": self.scouted, "result": None,
                "score": {}, "rendering": "tactical",
                "warnings": ["接口沙盒：这是确定性测试状态，不是真实星际游戏或模型验收证据。"]}

    def _result(self, receipts: list) -> dict:
        return {"observation": self._observation(), "receipts": receipts, "frame_path": None, "terminal": False}

    async def step(self, commands: list[dict], advance_loops: int) -> dict:
        if self.closed:
            raise RuntimeError("Sandbox already closed")
        receipts = []
        effects = {"train_worker": "SCV", "train_marine": "MARINE", "build_supply": "SUPPLYDEPOT",
                   "build_barracks": "BARRACKS", "build_engineering_bay": "ENGINEERINGBAY",
                   "build_refinery": "REFINERY", "expand": "COMMANDCENTER"}
        for command in commands:
            tool = command["tool"]
            spec = TOOLS.get(tool)
            reason = None
            if spec is None:
                reason = "unknown_tool"
            elif self.minerals < spec[1] or self.vespene < spec[2]:
                reason = "insufficient_resources"
            elif tool == "train_marine" and not self.buildings.get("BARRACKS"):
                reason = "requires_ready_barracks"
            elif tool == "research_infantry" and not self.buildings.get("ENGINEERINGBAY"):
                reason = "requires_ready_engineering_bay"
            elif tool in {"train_worker", "train_marine"} and sum(self.counts.values()) >= self.supply_cap:
                reason = "supply_blocked"
            if reason:
                receipts.append({**command, "status": "rejected", "reason": reason, "details": {"engine": "sandbox"}})
                continue
            self.minerals -= spec[1]
            self.vespene -= spec[2]
            if tool in effects:
                self.pending.append((self.loop + 224, effects[tool], command["command_id"]))
            elif tool == "scout":
                self.pending.append((self.loop + 224, "SCOUT", command["command_id"]))
            receipts.append({**command, "status": "accepted", "reason": "sandbox_order_accepted",
                             "details": {"engine": "sandbox", "not_native_evidence": True}})
        self.loop += advance_loops
        self.minerals += self.counts["SCV"] * advance_loops / 22.4 * 0.7
        self.vespene += self.buildings.get("REFINERY", 0) * advance_loops / 22.4 * 1.2
        ready = [p for p in self.pending if p[0] <= self.loop]
        self.pending = [p for p in self.pending if p[0] > self.loop]
        for _, effect, _ in ready:
            if effect == "SCOUT":
                self.scouted = True
            elif effect in self.counts:
                self.counts[effect] += 1
            else:
                self.buildings[effect] = self.buildings.get(effect, 0) + 1
                if effect == "SUPPLYDEPOT":
                    self.supply_cap += 8
        return self._result(receipts)

    async def close(self) -> dict:
        self.closed = True
        return {"closed": True, "replay": None, "engine": "sandbox"}

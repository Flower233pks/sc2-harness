"""Native mechanical SC2 executor. No automatic strategy and no model access.

Public native observation additions:
* ready_counts/pending_counts count existing owned entities by build_progress;
  queued_counts counts unfinished training orders, never accepted proposals.
* units[].orders and production[].orders preserve native ability/progress/targets;
  production includes readiness, busy state and the observed queue length.
* worker_state.eligible_builder_tags mirrors the executor's idle/harvesting filter;
  action_state[tool] additionally uses one batch of current native abilities,
  resources and prerequisites. Placement/destination still requires execution checks.
* upgrades.completed comes only from native upgrade_ids; in_progress comes from
  actual research orders. research_infantry affordance describes its default weapons-I.
* score preserves native cumulative resource and kill/loss values. An absent optional
  score field is None, not a manufactured zero or an inferred number of deaths.
* Own structures expose optional native assigned_harvesters/ideal_harvesters.
  worker_state resource counts reflect current order targets; returning cargo with
  no exposed origin is unknown. Gas assignments respect reported remaining capacity,
  observed en-route orders and same-turn reservations; workers_commanded counts
  accepted assignment commands, not confirmed arrivals or subsequent gas collection.
  Explicit mineral gathering uses idle SCVs first, then directly observed own-gas
  harvesters; construction, movement, unknown return origins and scouts are protected.
* map.resource_diagnostics contains only neutral resources actually returned by the
  API. Snapshot positions may be known while contents are unknown; visible empty
  mineral fields are excluded from expansion candidates. Diagnostics are debug-only.
* scout keeps its dispatched unit identity across movement orders. Its affordance
  includes default_unit_tag/default_available; explicit legal unit_tag can reassign,
  while a busy live default scout is never silently replaced by another worker.
* Building receipts include pathing evidence for a reachable SCV/footprint approach.
  Initial accepted/engine_results are ACKs; execution_status records uniquely
  associated later native failures (failed/partial/pending_observation). Unmatched
  unit+ability errors are public evidence, never guessed onto another command.
"""

from __future__ import annotations

import asyncio
import ctypes.util
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROLE_TOOLS = {
    "economy": {"train_worker", "gather", "build_supply", "build_refinery", "expand"},
    "production": {"build_barracks", "build_engineering_bay", "train_marine", "research_infantry"},
    "tactics": {"scout", "attack", "retreat", "hold"},
}
WORKERS = {"SCV", "MULE", "DRONE", "PROBE"}


def game_root() -> Path:
    configured = os.environ.get("SC2PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "StarCraft II"
    if sys.platform == "darwin":
        return Path("/Applications/StarCraft II")
    return Path.home() / "StarCraftII"


def probe() -> dict:
    root = game_root()
    try:
        version = importlib.metadata.version("burnysc2")
    except importlib.metadata.PackageNotFoundError:
        version = None
    binaries = (
        sorted(
            path
            for path in (root / "Versions").glob("Base*/SC2_x64*")
            if path.is_file() and path.name in {"SC2_x64", "SC2_x64.exe"}
        )
        if root.is_dir()
        else []
    )
    maps = sorted(path.stem for path in (root / "Maps").rglob("*.SC2Map")) if root.is_dir() else []
    ready = version == "6.6.0" and bool(binaries)
    reasons = []
    if version != "6.6.0":
        reasons.append("burnysc2_6_6_0_required")
    if not binaries:
        reasons.append("sc2_executable_missing")
    return {
        "available": ready,
        "engine": "sc2",
        "python": sys.executable,
        "sdk": version,
        "game_path": str(root),
        "executables": [str(path) for path in binaries],
        "maps": maps,
        "reason": ", ".join(reasons) or None,
        "native_verified": False,
    }


def destination(args: dict, default: tuple[float, float], width: float, height: float) -> tuple[float, float]:
    if ("x" in args) != ("y" in args):
        raise ValueError("Destination requires both x and y")
    values = (args.get("x", default[0]), args.get("y", default[1]))
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("Destination coordinates must be numbers")
    x, y = map(float, values)
    if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= width or not 0 <= y <= height:
        raise ValueError("Destination is outside the game map")
    return x, y


def validate_command(command: dict) -> None:
    if not isinstance(command, dict):
        raise TypeError("Command must be an object")
    identity = command.get("command_id")
    if not isinstance(identity, str) or not identity or len(identity) > 200:
        raise ValueError("Command requires a bounded command_id")
    role = command.get("actor_id")
    tool = command.get("tool")
    if not isinstance(role, str) or not isinstance(tool, str) or tool not in ROLE_TOOLS.get(role, set()):
        raise ValueError("Tool is not authorized for this role")
    args = command.get("args", {})
    if not isinstance(args, dict):
        raise TypeError("Tool args must be an object")
    allowed = {
        "gather": {"unit_tag", "resource", "max_workers"},
        "research_infantry": {"upgrade"},
        "scout": {"unit_tag", "x", "y"},
        "attack": {"x", "y"},
        "retreat": {"x", "y"},
        "build_supply": {"x", "y"},
        "build_refinery": {"x", "y"},
        "build_barracks": {"x", "y"},
        "build_engineering_bay": {"x", "y"},
        "expand": {"x", "y"},
    }.get(tool, set())
    if set(args) - allowed:
        raise ValueError("Unsupported tool argument")
    if "unit_tag" in args:
        tag = args["unit_tag"]
        if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
            raise ValueError("unit_tag must be a positive integer")
    for key in ("resource", "upgrade"):
        if key in args and not isinstance(args[key], str):
            raise ValueError(f"{key} must be text")


class NativeSession:
    def __init__(self) -> None:
        self.config: dict = {}
        self.run_dir: Path | None = None
        self.process: Any = None
        self.client: Any = None
        self.controller: Any = None
        self.data: Any = None
        self.info: Any = None
        self.response: Any = None
        self.player_id: int | None = None
        self.scouted = False
        self.warnings: list[str] = []
        self.frame_path: Path | None = None
        self.frame_loop: int | None = None
        self.engine_result: str | None = None
        self.closed = False
        self.close_result: dict = {"status": "not_started", "replay": None}
        self.seen_commands: set[str] = set()
        self.start_point = (0.0, 0.0)
        self.enemy_start = (0.0, 0.0)
        self.width = self.height = 0
        self.types: dict[int, Any] = {}
        self.reserved = {"minerals": 0, "vespene": 0, "supply": 0.0}
        self.busy: set[int] = set()
        self.engine_identity: dict = {}
        self.action_state: dict = {}
        self.gas_reserved: dict[int, int] = {}
        self.scout_tag: int | None = None
        self.unmatched_action_errors: list[dict] = []

    async def start(self, config: dict, run_dir: str) -> dict:
        if self.process is not None or self.closed:
            raise RuntimeError("Native session cannot be restarted")
        availability = probe()
        if not availability["available"]:
            raise RuntimeError(f"Native SC2 unavailable: {availability['reason']}")
        self.config = dict(config)
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        map_name = config.get("map_name", "Simple64")
        if not isinstance(map_name, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,100}", map_name):
            raise ValueError("Invalid map name")
        # Optional dependencies are loaded only after a real start request.
        from s2clientprotocol import common_pb2, query_pb2, raw_pb2, sc2api_pb2
        from sc2 import maps
        from sc2.client import Client
        from sc2.data import Difficulty, Race
        from sc2.sc2process import SC2Process

        class OwnedSC2Process(SC2Process):
            def _clean(process_self, verbose=True):
                # Burny 6.6 also invokes `wineserver -k` in Linux cleanup. This
                # worker owns only its exact child; never stop another session.
                native = process_self._process
                if native is not None:
                    if native.poll() is None:
                        native.terminate()
                        try:
                            native.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            native.kill()
                            native.wait(timeout=2)
                    else:
                        native.wait()
                temporary = Path(process_self._tmp_dir).resolve()
                temporary_root = Path(tempfile.gettempdir()).resolve()
                if temporary.parent == temporary_root and temporary.name.startswith("SC2_"):
                    shutil.rmtree(temporary, ignore_errors=True)
                process_self._process = None
                process_self._ws = None
                if process_self._used_portpicker and process_self._port is not None:
                    import portpicker

                    portpicker.return_port(process_self._port)
                    process_self._port = None

        self.pb = sc2api_pb2
        self.raw_pb = raw_pb2
        self.query_pb = query_pb2
        self.common_pb = common_pb2
        self.map = maps.get(map_name)
        self.process = OwnedSC2Process(render=False)
        want_rgb = self._configure_renderer()
        try:
            self.controller = await asyncio.wait_for(self.process.__aenter__(), 120)
            ping = (await self.controller.ping()).ping
            self.engine_identity = {
                "game_version": ping.game_version,
                "base_build": ping.base_build,
                "data_build": ping.data_build,
                "data_version": ping.data_version,
                "sdk": "burnysc2==6.6.0",
                "native_pid": self.process._process.pid,
                "map_name": map_name,
                "map_sha256": hashlib.sha256(self.map.data).hexdigest(),
                "native_player_count": 1,
                "opponent": "builtin_terran_very_easy",
                "clock": "non_realtime_manual_step",
            }
            (self.run_dir / "native-engine.json").write_text(
                json.dumps(self.engine_identity, indent=2),
                encoding="utf-8",
            )
            # SDK 6.6 only sends a relative map path. Linux SC2 resolves it
            # beneath lowercase `maps`, whereas official packs use `Maps`.
            # Supplying the actual map bytes avoids a case-sensitive filesystem
            # dependency and keeps the original game installation untouched.
            created = await self.controller._execute(
                create_game=sc2api_pb2.RequestCreateGame(
                    local_map=sc2api_pb2.LocalMap(map_path=str(self.map.path), map_data=self.map.data),
                    player_setup=[
                        sc2api_pb2.PlayerSetup(type=sc2api_pb2.Participant),
                        sc2api_pb2.PlayerSetup(
                            type=sc2api_pb2.Computer,
                            race=Race.Terran.value,
                            difficulty=Difficulty.VeryEasy.value,
                        ),
                    ],
                    realtime=False,
                    random_seed=int(config.get("seed", 7)),
                    disable_fog=False,
                ),
            )
            if created.create_game.HasField("error"):
                raise RuntimeError(f"SC2 create failed: {created.create_game.error_details}")
            self.client = Client(self.controller._ws)
            options = sc2api_pb2.InterfaceOptions(
                raw=True,
                score=True,
                raw_affects_selection=False,
                show_cloaked=False,
                show_burrowed_shadows=False,
                show_placeholders=True,
            )
            if want_rgb:
                options.render.resolution.x = 960
                options.render.resolution.y = 540
                options.render.minimap_resolution.x = 128
                options.render.minimap_resolution.y = 128
            joined = await self.client._execute(
                join_game=sc2api_pb2.RequestJoinGame(race=Race.Terran.value, options=options),
            )
            if joined.join_game.HasField("error") or not joined.join_game.player_id:
                raise RuntimeError(f"SC2 join failed: {joined.join_game.error_details}")
            self.player_id = int(joined.join_game.player_id)
            self.client._player_id = self.player_id
            self.info = (await self.client._execute(game_info=sc2api_pb2.RequestGameInfo())).game_info
            self.data = (
                await self.client._execute(
                    data=sc2api_pb2.RequestData(
                        ability_id=True,
                        unit_type_id=True,
                        upgrade_id=True,
                    )
                )
            ).data
            self.types = {unit.unit_id: unit for unit in self.data.units}
            self.width = self.info.start_raw.map_size.x
            self.height = self.info.start_raw.map_size.y
            await self._observe()
            own_bases = [
                unit for unit in self._own() if self._name(unit) in {"COMMANDCENTER", "NEXUS", "HATCHERY"}
            ]
            if not own_bases:
                raise RuntimeError("Map does not contain a native starting base; full-melee map required")
            self.start_point = (own_bases[0].pos.x, own_bases[0].pos.y)
            starts = [(point.x, point.y) for point in self.info.start_raw.start_locations]
            if not starts:
                raise RuntimeError("Map has no opponent start locations")
            self.enemy_start = max(starts, key=lambda point: math.dist(point, self.start_point))
            await self._refresh_action_state()
            return self._result([])
        except BaseException:
            await self.close()
            raise

    def _configure_renderer(self) -> bool:
        if sys.platform != "linux":
            return True
        backend = os.environ.get("SC2H_RENDER_BACKEND", "software").lower()
        if backend == "none":
            self.warnings.append("Native RGB disabled by explicit configuration")
            return False
        library = ctypes.util.find_library("EGL" if backend == "egl" else "OSMesa")
        if library:
            self.process._arguments["-eglpath" if backend == "egl" else "-osmesapath"] = library
            return True
        self.warnings.append("Native RGB unavailable: requested render library is missing")
        return False

    def _name(self, unit: Any) -> str:
        from sc2.ids.unit_typeid import UnitTypeId

        try:
            return UnitTypeId(unit.unit_type).name
        except ValueError:
            return f"UNIT_{unit.unit_type}"

    def _own(self) -> list[Any]:
        return [unit for unit in self.response.observation.raw_data.units if unit.owner == self.player_id]

    def _is_structure(self, unit: Any) -> bool:
        # Attribute.Structure = 8 in the official data protocol.
        definition = self.types.get(unit.unit_type)
        return definition is not None and 8 in definition.attributes

    def _workers(self) -> list[Any]:
        return [unit for unit in self._own() if self._name(unit) == "SCV" and unit.build_progress >= 1]

    def _army(self) -> list[Any]:
        return [
            unit
            for unit in self._own()
            if not self._is_structure(unit) and self._name(unit) not in WORKERS and unit.build_progress >= 1
        ]

    @staticmethod
    def _harvest_abilities() -> set[int]:
        from sc2.ids.ability_id import AbilityId

        return {AbilityId[name].value for name in (
            "HARVEST_GATHER_SCV", "HARVEST_RETURN_SCV", "HARVEST_GATHER", "HARVEST_RETURN"
        )}

    def _eligible_builders(self, *, respect_reservations: bool = True) -> list[Any]:
        harvest = self._harvest_abilities()
        return [unit for unit in self._workers()
                if (not respect_reservations or unit.tag not in self.busy)
                and all(order.ability_id in harvest for order in unit.orders)]

    def _gather_candidates(self, resource: str, *, respect_reservations: bool = True) -> list[Any]:
        """Eligible idle or explicitly identified opposite-resource harvesters.

        A return-cargo order naming a town hall does not reveal its origin. Never
        infer that it is a gas worker or interrupt unrelated movement/construction.
        The tracked scout retains its assignment even when it becomes idle.
        """
        harvest = self._harvest_abilities()
        transfer_targets = (
            {int(unit.tag) for unit in self._own()
             if self._name(unit) == "REFINERY" and unit.build_progress >= 1}
            if resource == "minerals" else
            {int(unit.tag) for unit in self.response.observation.raw_data.units if unit.mineral_contents > 0}
        )
        return [unit for unit in self._workers()
                if unit.tag != self.scout_tag
                and (not respect_reservations or unit.tag not in self.busy)
                and (not unit.orders or all(order.ability_id in harvest
                                            and order.target_unit_tag in transfer_targets for order in unit.orders))]

    def _tracked_scout(self) -> Any:
        return next((unit for unit in self._own() if unit.tag == self.scout_tag), None)

    def _is_constructing_worker(self, unit: Any) -> bool:
        if self._name(unit) != "SCV":
            return False
        construction = {item.ability_id for item in self.types.values() if 8 in item.attributes}
        unfinished = {int(item.tag) for item in self._own()
                      if self._is_structure(item) and item.build_progress < 1}
        return any(order.ability_id in construction or order.target_unit_tag in unfinished
                   for order in unit.orders)

    def _scout_candidates(self, *, respect_reservations: bool = True) -> list[Any]:
        """A dispatched scout may keep moving; unrelated moving SCVs stay protected."""
        tracked = self._tracked_scout()
        candidates = ([tracked] if tracked is not None else []) + self._army() + self._eligible_builders(
            respect_reservations=respect_reservations)
        result = []
        seen = set()
        for unit in candidates:
            if (unit.tag in seen or unit.build_progress < 1 or self._is_structure(unit)
                    or self._is_constructing_worker(unit)
                    or (respect_reservations and unit.tag in self.busy)):
                continue
            seen.add(unit.tag)
            result.append(unit)
        return result

    def _ability_available(self, abilities: set[int], ability: int) -> bool:
        remaps = {item.ability_id: item.remaps_to_ability_id for item in getattr(self.data, "abilities", [])
                  if item.remaps_to_ability_id}
        target = remaps.get(ability, ability)
        return any(remaps.get(item, item) == target for item in abilities)

    def _orders(self, unit: Any) -> list[dict]:
        """Raw observed orders; progress is not estimated and training is not a unit yet."""
        from sc2.ids.ability_id import AbilityId
        from sc2.ids.upgrade_id import UpgradeId

        result = []
        for order in unit.orders:
            try:
                ability_name = AbilityId(order.ability_id).name
            except ValueError:
                ability_name = f"ABILITY_{order.ability_id}"
            produced = next((item for item in self.types.values() if item.ability_id == order.ability_id), None)
            research = next((item for item in getattr(self.data, "upgrades", [])
                             if item.ability_id == order.ability_id), None)
            upgrade_name = None
            if research is not None:
                try:
                    upgrade_name = UpgradeId(research.upgrade_id).name
                except ValueError:
                    upgrade_name = f"UPGRADE_{research.upgrade_id}"
            result.append({
                "ability_id": int(order.ability_id), "ability": ability_name,
                "progress": float(order.progress) if order.HasField("progress") else None,
                "target_unit_tag": int(order.target_unit_tag) if order.HasField("target_unit_tag") else None,
                "target": [order.target_world_space_pos.x, order.target_world_space_pos.y]
                if order.HasField("target_world_space_pos") else None,
                "produces": self._type_name(produced.unit_id) if produced is not None else None,
                "researches": upgrade_name,
            })
        return result

    @staticmethod
    def _type_name(unit_type: int) -> str:
        from sc2.ids.unit_typeid import UnitTypeId

        try:
            return UnitTypeId(unit_type).name
        except ValueError:
            return f"UNIT_{unit_type}"

    def _upgrades(self) -> dict:
        from sc2.ids.upgrade_id import UpgradeId

        completed = []
        for identity in self.response.observation.raw_data.player.upgrade_ids:
            try:
                completed.append(UpgradeId(identity).name)
            except ValueError:
                completed.append(f"UPGRADE_{identity}")
        in_progress = [
            {"upgrade": order["researches"], "unit_tag": int(unit.tag),
             "ability_id": order["ability_id"], "progress": order["progress"]}
            for unit in self._own() for order in self._orders(unit)
            if order["researches"] and order["researches"] not in completed
        ]
        return {"completed": completed, "in_progress": in_progress}

    def _worker_state(self) -> dict:
        """Eligibility mirrors the executor's order filter, not just is_idle."""
        from sc2.ids.ability_id import AbilityId

        workers = self._workers()
        harvest = self._harvest_abilities()
        construction = {item.ability_id for item in self.types.values() if 8 in item.attributes}
        moving = {AbilityId.MOVE_MOVE.value, AbilityId.MOVE.value}
        eligible = [int(unit.tag) for unit in self._eligible_builders(respect_reservations=False)]
        mineral_targets = {int(unit.tag) for unit in self.response.observation.raw_data.units
                           if unit.mineral_contents > 0}
        mineral_assignments = {}
        for unit in workers:
            for order in unit.orders:
                if order.ability_id in harvest and order.target_unit_tag in mineral_targets:
                    mineral_assignments.setdefault(int(order.target_unit_tag), set()).add(int(unit.tag))
        mineral_tags = set().union(*mineral_assignments.values()) if mineral_assignments else set()
        gas_assignments = self._gas_capacity(respect_reservations=False)
        gas_tags = {tag for value in gas_assignments for tag in value["worker_tags"]}
        harvest_tags = {int(unit.tag) for unit in workers if unit.orders
                        and all(order.ability_id in harvest for order in unit.orders)}
        return {
            "idle_tags": [int(unit.tag) for unit in workers if not unit.orders],
            "harvesting_tags": [int(unit.tag) for unit in workers if unit.orders
                                and all(order.ability_id in harvest for order in unit.orders)],
            "constructing_tags": [int(unit.tag) for unit in workers
                                  if any(order.ability_id in construction for order in unit.orders)],
            "moving_tags": [int(unit.tag) for unit in workers
                            if any(order.ability_id in moving for order in unit.orders)],
            "eligible_builder_tags": eligible, "eligible_builder_count": len(eligible),
            "mineral_worker_count": len(mineral_tags), "gas_worker_count": len(gas_tags),
            "unknown_harvest_worker_count": len(harvest_tags - mineral_tags - gas_tags),
            "mineral_assignments": [
                {"target_tag": tag, "worker_count": len(tags), "worker_tags": sorted(tags)}
                for tag, tags in sorted(mineral_assignments.items())
            ],
            "gas_assignments": gas_assignments,
        }

    @staticmethod
    def _optional_int(message: Any, field: str) -> int | None:
        return int(getattr(message, field)) if message.HasField(field) else None

    def _gas_capacity(self, *, respect_reservations: bool = True) -> list[dict]:
        """Capacity is native, with observed en-route orders and this turn's reservations.

        Unknown counts remain unknown. Returning cargo has no exposed origin, so
        order-target worker counts never replace the native assigned total.
        """
        harvest = self._harvest_abilities()
        result = []
        for refinery in self._own():
            if self._name(refinery) != "REFINERY" or refinery.build_progress < 1:
                continue
            tags = sorted({int(worker.tag) for worker in self._workers() if any(
                order.ability_id in harvest and order.target_unit_tag == refinery.tag for order in worker.orders)})
            assigned = self._optional_int(refinery, "assigned_harvesters")
            ideal = self._optional_int(refinery, "ideal_harvesters")
            reserved = self.gas_reserved.get(int(refinery.tag), 0) if respect_reservations else 0
            capacity = (None if assigned is None or ideal is None
                        else max(0, ideal - max(assigned, len(tags)) - reserved))
            result.append({"target_tag": int(refinery.tag), "worker_count": len(tags), "worker_tags": tags,
                           "assigned_harvesters": assigned, "ideal_harvesters": ideal,
                           "remaining_capacity": capacity})
        return sorted(result, key=lambda value: value["target_tag"])

    def _score(self) -> dict:
        """Preserve official cumulative values/categories; absent optional fields are null."""
        raw = self.response.observation
        score = raw.score
        details = score.score_details
        scalar = ("idle_production_time", "idle_worker_time", "total_value_units", "total_value_structures",
                  "killed_value_units", "killed_value_structures", "collected_minerals", "collected_vespene",
                  "collection_rate_minerals", "collection_rate_vespene", "spent_minerals", "spent_vespene")
        result = {"source": "sc2_native_score", "available": raw.HasField("score"),
                  "score": int(score.score) if score.HasField("score") else None,
                  "score_type": type(score).ScoreType.Name(score.score_type)
                  if score.HasField("score_type") else None}
        result.update({name: float(getattr(details, name)) if details.HasField(name) else None for name in scalar})
        for name in ("killed_minerals", "killed_vespene", "lost_minerals", "lost_vespene"):
            category = getattr(details, name)
            result[name] = {key: float(getattr(category, key)) if category.HasField(key) else None
                            for key in ("none", "army", "economy", "technology", "upgrade")}
        return result

    async def _refresh_action_state(self) -> None:
        """One native batch query per public decision state, never one query per unit.

        Affordances cover current ownership, readiness, orders, resources and native
        prerequisites. Argument-specific placement/path/destinations are still
        checked by the tool's native query and ultimately by the engine.
        """
        from sc2.ids.ability_id import AbilityId

        own = [unit for unit in self._own() if unit.build_progress >= 1]
        reply = await self.client._execute(query=self.query_pb.RequestQuery(
            abilities=[self.query_pb.RequestQueryAvailableAbilities(unit_tag=unit.tag) for unit in own],
            ignore_resource_requirements=False,
        )) if own and self.engine_result is None else None
        native = {int(item.unit_tag): {int(ability.ability_id) for ability in item.abilities}
                  for item in reply.query.abilities} if reply is not None else {}
        common = self.response.observation.player_common
        builders = self._eligible_builders(respect_reservations=False)
        upgrades = self._upgrades()
        state = {}

        def add(tool, candidates, ability, reason=None, cost=None):
            if reason is None and cost is not None:
                if common.minerals < cost.mineral_cost:
                    reason = "insufficient_minerals"
                elif common.vespene < cost.vespene_cost:
                    reason = "insufficient_vespene"
                elif getattr(cost, "food_required", 0) > common.food_cap - common.food_used:
                    reason = "insufficient_supply"
            tags = sorted({int(unit.tag) for unit in candidates
                           if self._ability_available(native.get(int(unit.tag), set()), ability)}) if ability else []
            if self.engine_result is not None:
                reason = "game_ended"
            elif reason is None and not tags:
                reason = "no_eligible_unit_or_native_ability"
            state[tool] = {"available": reason is None and bool(tags), "unit_tags": tags if reason is None else [],
                           "reason": reason or "native_ability_available", "game_loop": int(self.response.observation.game_loop),
                           "source": "native_available_abilities"}

        for tool, name, producers in (
            ("train_worker", "SCV", {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS"}),
            ("train_marine", "MARINE", {"BARRACKS"}),
        ):
            definition = self._definition(name)
            candidates = [unit for unit in own if self._name(unit) in producers and not unit.orders]
            add(tool, candidates, definition.ability_id, cost=definition)
        for tool, name in (("build_supply", "SUPPLYDEPOT"), ("build_refinery", "REFINERY"),
                           ("build_barracks", "BARRACKS"), ("build_engineering_bay", "ENGINEERINGBAY"),
                           ("expand", "COMMANDCENTER")):
            definition = self._definition(name)
            reason = None
            if name == "REFINERY" and not any(
                unit.vespene_contents > 0 and unit.owner != self.player_id and all(
                    math.dist((unit.pos.x, unit.pos.y), (refinery.pos.x, refinery.pos.y)) > 2
                    for refinery in own if self._name(refinery) == "REFINERY")
                for unit in self.response.observation.raw_data.units
            ):
                reason = "no_unoccupied_visible_geyser"
            if name == "COMMANDCENTER" and not any(
                all(math.dist(point, (base.pos.x, base.pos.y)) > 16
                    for base in own if self._name(base) in {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS"})
                for point in self._expansions()
            ):
                reason = "no_known_expansion_candidate"
            add(tool, builders, definition.ability_id, reason=reason, cost=definition)
        mineral_targets = {int(unit.tag) for unit in self.response.observation.raw_data.units
                           if unit.mineral_contents > 0}
        mineral_workers = self._gather_candidates("minerals", respect_reservations=False) if mineral_targets else []
        gas_workers = self._gather_candidates("gas", respect_reservations=False)
        if not any(value["remaining_capacity"] is not None and value["remaining_capacity"] > 0
                   for value in self._gas_capacity(respect_reservations=False)):
            gas_workers = []
        gather_ability = AbilityId.HARVEST_GATHER_SCV.value
        add("gather", mineral_workers + gas_workers, gather_ability)
        state["gather"]["resources"] = [resource for resource, candidates in (
            ("minerals", mineral_workers), ("gas", gas_workers)) if any(
                self._ability_available(native.get(int(unit.tag), set()), gather_ability) for unit in candidates)]
        research = next((item for item in getattr(self.data, "upgrades", []) if item.upgrade_id == 7), None)
        reason = "upgrade_data_unavailable" if research is None else None
        if "TERRANINFANTRYWEAPONSLEVEL1" in upgrades["completed"]:
            reason = "upgrade_already_complete"
        elif any(item["upgrade"] == "TERRANINFANTRYWEAPONSLEVEL1" for item in upgrades["in_progress"]):
            reason = "upgrade_in_progress"
        add("research_infantry", [unit for unit in own if self._name(unit) == "ENGINEERINGBAY" and not unit.orders],
            research.ability_id if research else 0, reason=reason, cost=research)
        army = self._army()
        scout_candidates = self._scout_candidates(respect_reservations=False)
        add("scout", scout_candidates, AbilityId.MOVE_MOVE.value)
        tracked = self._tracked_scout()
        default_scout = tracked if tracked is not None else next(iter(scout_candidates), None)
        state["scout"]["default_unit_tag"] = int(default_scout.tag) if default_scout is not None else None
        state["scout"]["default_available"] = (
            default_scout is not None and int(default_scout.tag) in state["scout"]["unit_tags"])
        add("retreat", army, AbilityId.MOVE_MOVE.value)
        add("attack", army, AbilityId.ATTACK_ATTACK.value)
        add("hold", army, AbilityId.HOLDPOSITION_HOLD.value)
        self.action_state = state

    async def _observe(self) -> None:
        response = await self.client.observation()
        self.response = response.observation
        for result in self.response.player_result:
            if result.player_id == self.player_id:
                self.engine_result = self.pb.Result.Name(result.result)
        if self.enemy_start != (0.0, 0.0) and any(
            math.dist((unit.pos.x, unit.pos.y), self.enemy_start) < 10 for unit in self._own()
        ):
            self.scouted = True
        self._capture_frame()

    def _capture_frame(self) -> None:
        observation = self.response.observation
        if not observation.HasField("render_data") or not observation.render_data.HasField("map"):
            return
        image = observation.render_data.map
        if not image.data:
            return
        from PIL import Image

        width, height = image.size.x, image.size.y
        if not 0 < width <= 4096 or not 0 < height <= 4096 or image.bits_per_pixel not in {24, 32}:
            raise RuntimeError("SC2 returned an invalid native RGB image")
        mode = "RGB" if image.bits_per_pixel == 24 else "RGBA"
        if len(image.data) != width * height * (image.bits_per_pixel // 8):
            raise RuntimeError("SC2 returned a truncated native RGB image")
        frame = Image.frombytes(mode, (width, height), image.data).convert("RGB")
        assert self.run_dir is not None
        temporary = self.run_dir / "native-frame.pending.jpg"
        frame.save(temporary, format="JPEG", quality=85)
        self.frame_path = self.run_dir / "native-frame.jpg"
        temporary.replace(self.frame_path)
        self.frame_loop = int(observation.game_loop)

    def _observation(self) -> dict:
        raw = self.response.observation
        common = raw.player_common
        visible = [
            unit
            for unit in raw.raw_data.units
            if unit.owner == self.player_id or (unit.alliance == 4 and unit.display_type == 1)
        ]
        units = [
            {
                "tag": int(unit.tag),
                "type": self._name(unit),
                "x": unit.pos.x,
                "y": unit.pos.y,
                "health": unit.health,
                "health_max": unit.health_max,
                "shield": unit.shield,
                "owner": "self" if unit.owner == self.player_id else "enemy",
                "owner_id": int(unit.owner),
                "is_structure": self._is_structure(unit),
                "is_idle": len(unit.orders) == 0,
                "is_ready": unit.build_progress >= 1,
                "build_progress": float(unit.build_progress),
                "orders": self._orders(unit) if unit.owner == self.player_id else None,
                "assigned_harvesters": self._optional_int(unit, "assigned_harvesters")
                if unit.owner == self.player_id and self._is_structure(unit) else None,
                "ideal_harvesters": self._optional_int(unit, "ideal_harvesters")
                if unit.owner == self.player_id and self._is_structure(unit) else None,
            }
            for unit in visible
        ]
        counts: dict[str, int] = {}
        structures: dict[str, int] = {}
        ready: dict[str, int] = {}
        pending: dict[str, int] = {}
        queued: dict[str, int] = {}
        production = []
        for unit in self._own():
            name = self._name(unit)
            counts[name] = counts.get(name, 0) + 1
            target = ready if unit.build_progress >= 1 else pending
            target[name] = target.get(name, 0) + 1
            if self._is_structure(unit) and unit.build_progress >= 1:
                structures[name] = structures.get(name, 0) + 1
            if self._is_structure(unit):
                orders = self._orders(unit)
                for order in orders:
                    definition = next((item for item in self.types.values()
                                       if item.ability_id == order["ability_id"]), None)
                    if definition is not None and 8 not in definition.attributes:
                        produced = self._type_name(definition.unit_id)
                        queued[produced] = queued.get(produced, 0) + 1
                if name in {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS", "BARRACKS",
                            "FACTORY", "STARPORT", "ENGINEERINGBAY", "ARMORY"}:
                    production.append({"tag": int(unit.tag), "type": name, "is_ready": unit.build_progress >= 1,
                                       "is_busy": bool(unit.orders), "queue_length": len(unit.orders), "orders": orders})
        current_rgb = self.frame_path is not None and self.frame_loop == raw.game_loop
        return {
            "engine": "sc2",
            "engine_identity": dict(self.engine_identity),
            "game_loop": int(raw.game_loop),
            "game_time": raw.game_loop / 22.4,
            "player_id": self.player_id,
            "minerals": common.minerals,
            "vespene": common.vespene,
            "supply_used": common.food_used,
            "supply_cap": common.food_cap,
            "workers": common.food_workers,
            "army_count": common.army_count,
            "units": units,
            "structures": structures,
            "unit_counts": counts,
            "ready_counts": ready,
            "pending_counts": pending,
            "queued_counts": queued,
            "production": production,
            "worker_state": self._worker_state(),
            "upgrades": self._upgrades(),
            "action_state": self.action_state,
            "unmatched_action_errors": list(self.unmatched_action_errors),
            "map": {
                "width": self.width,
                "height": self.height,
                "start": list(self.start_point),
                "enemy_start": list(self.enemy_start),
                "expansions": self._expansions(),
                "resource_diagnostics": self._resource_diagnostics(),
            },
            "enemy_visible": sum(unit.alliance == 4 and unit.display_type == 1 for unit in visible),
            "scouted": self.scouted,
            "result": self.engine_result,
            "score": self._score(),
            "rendering": "rgb" if current_rgb else "unavailable",
            "warnings": list(self.warnings),
        }

    def _mineral_fields(self) -> list[Any]:
        """Use API-provided neutral types/positions, without inventing fog contents.

        SC2 may expose neutral mineral snapshots with no current contents. A
        visible resource explicitly reporting zero is depleted and excluded.
        """
        result = []
        for unit in self.response.observation.raw_data.units:
            definition = self.types.get(unit.unit_type)
            mineral_type = definition is not None and definition.has_minerals
            if not (unit.alliance == 3 or unit.owner == 16) or not (mineral_type or unit.mineral_contents > 0):
                continue
            if unit.display_type == 1 and unit.HasField("mineral_contents") and unit.mineral_contents == 0:
                continue
            result.append(unit)
        return result

    def _resource_diagnostics(self) -> dict:
        """Debug-only raw neutral evidence; omit this detailed list from model prompts."""
        result = []
        for unit in self.response.observation.raw_data.units:
            if not (unit.alliance == 3 or unit.owner == 16):
                continue
            definition = self.types.get(unit.unit_type)
            has_minerals = bool(definition is not None and definition.has_minerals)
            has_vespene = bool(definition is not None and definition.has_vespene)
            if not (has_minerals or has_vespene or unit.mineral_contents > 0 or unit.vespene_contents > 0):
                continue
            mineral_reported = unit.HasField("mineral_contents")
            gas_reported = unit.HasField("vespene_contents")
            result.append({
                "tag": int(unit.tag), "type": self._name(unit), "x": unit.pos.x, "y": unit.pos.y,
                "display_type": int(unit.display_type), "has_minerals": has_minerals,
                "has_vespene": has_vespene,
                "mineral_contents": int(unit.mineral_contents) if mineral_reported else None,
                "mineral_contents_reported": mineral_reported,
                "mineral_contents_known": unit.display_type == 1 and mineral_reported,
                "vespene_contents": int(unit.vespene_contents) if gas_reported else None,
                "vespene_contents_reported": gas_reported,
                "vespene_contents_known": unit.display_type == 1 and gas_reported,
                "depleted": has_minerals and unit.display_type == 1 and mineral_reported
                and unit.mineral_contents == 0,
            })
        return {"source": "native_raw_neutral_units", "total_resources": len(result),
                "resources": sorted(result, key=lambda value: value["tag"])[:512], "truncated": len(result) > 512}

    def _expansions(self) -> list[list[float]]:
        resources = self._mineral_fields()
        clusters: list[list[Any]] = []
        for resource in resources:
            matching = next(
                (
                    cluster
                    for cluster in clusters
                    if any(
                        math.dist((resource.pos.x, resource.pos.y), (other.pos.x, other.pos.y)) < 12
                        for other in cluster
                    )
                ),
                None,
            )
            if matching is None:
                clusters.append([resource])
            else:
                matching.append(resource)
        return [
            [
                sum(unit.pos.x for unit in cluster) / len(cluster),
                sum(unit.pos.y for unit in cluster) / len(cluster),
            ]
            for cluster in clusters
        ]

    def _result(self, receipts: list[dict]) -> dict:
        observation = self._observation()
        return {
            "observation": observation,
            "receipts": receipts,
            "frame_path": str(self.frame_path) if observation["rendering"] == "rgb" else None,
            "terminal": self.engine_result is not None,
        }

    async def step(self, commands: list[dict], advance_loops: int) -> dict:
        if self.client is None or self.closed:
            raise RuntimeError("Native session has not started")
        if not isinstance(commands, list) or len(commands) > 6:
            raise ValueError("At most six commands are accepted per decision round")
        if (
            isinstance(advance_loops, bool)
            or not isinstance(advance_loops, int)
            or not 1 <= advance_loops <= 2240
        ):
            raise ValueError("Invalid advance_loops")
        if self.engine_result is not None:
            return self._result(
                [self._receipt(command, "rejected", "Game already ended") for command in commands]
            )
        self.reserved = {"minerals": 0, "vespene": 0, "supply": 0.0}
        self.busy = set()
        self.gas_reserved = {}
        self.unmatched_action_errors = []
        receipts = []
        for command in commands:
            try:
                validate_command(command)
                if command["command_id"] in self.seen_commands:
                    raise ValueError("Duplicate command_id; command was not replayed")
                self.seen_commands.add(command["command_id"])
                details = await self._tool(command["tool"], command.get("args", {}))
                receipts.append(self._receipt(command, "accepted", "SC2 accepted native action", details))
            except (ValueError, TypeError) as exc:
                receipts.append(self._receipt(command, "rejected", str(exc), getattr(exc, "details", None)))
        remaining = advance_loops
        errors = []
        while remaining and self.engine_result is None:
            stride = min(8, remaining)
            await self.client.step(stride)
            await self._observe()
            errors.extend(
                {"unit_tag": int(error.unit_tag), "ability_id": int(error.ability_id),
                 "result": int(error.result), "game_loop": int(self.response.observation.game_loop)}
                for error in self.response.action_errors
            )
            remaining -= stride
        self._associate_action_errors(receipts, errors)
        await self._refresh_action_state()
        return self._result(receipts)

    def _associate_action_errors(self, receipts: list[dict], errors: list[dict]) -> None:
        """Initial ACK stays intact; only unique unit+ability failures are attributed.

        The native error protocol has no command ID. Errors not uniquely matching
        an action submitted this turn stay unmatched rather than being guessed
        onto an older or unrelated command.
        """
        from sc2.data import ActionResult

        entries = []
        totals: dict[int, set[int]] = {}
        failed: dict[int, set[int]] = {}
        for index, receipt in enumerate(receipts):
            if receipt.get("status") != "accepted":
                continue
            details = receipt["details"]
            details["execution_status"] = "partial" if details.get("partial_failure") else "pending_observation"
            actions = [details] if "ability_id" in details else details.get("orders", [])
            for action in actions:
                if "ability_id" not in action:
                    continue
                for tag in action.get("unit_tags", []):
                    identity = len(entries)
                    entries.append((index, int(tag), int(action["ability_id"]), identity))
                    totals.setdefault(index, set()).add(identity)
        for error in errors:
            value = dict(error)
            value.pop("command_id", None)
            try:
                value["result_name"] = ActionResult(value["result"]).name
            except ValueError:
                value["result_name"] = f"UNKNOWN_RESULT_{value['result']}"
            matches = [entry for entry in entries if entry[1] == value["unit_tag"]
                       and self._ability_available({entry[2]}, value["ability_id"])]
            if len(matches) != 1:
                value["association_reason"] = "no_command_match" if not matches else "ambiguous_command_match"
                self.unmatched_action_errors.append(value)
                continue
            index, _tag, _ability, identity = matches[0]
            value["command_id"] = receipts[index]["command_id"]
            details = receipts[index]["details"]
            details.setdefault("native_errors", []).append(value)
            details.setdefault("observed_action_errors", []).append(value)
            failed.setdefault(index, set()).add(identity)
        for index, identities in failed.items():
            details = receipts[index]["details"]
            details["execution_status"] = "failed" if identities == totals[index] else "partial"
            details["failed_unit_tags"] = sorted({entry[1] for entry in entries if entry[3] in identities})
            details["pending_unit_tags"] = sorted({entry[1] for entry in entries
                                                    if entry[0] == index and entry[3] not in identities})

    @staticmethod
    def _receipt(command: Any, status: str, reason: str, details: dict | None = None) -> dict:
        command = command if isinstance(command, dict) else {}
        return {
            "command_id": command.get("command_id"),
            "actor_id": command.get("actor_id"),
            "tool": command.get("tool"),
            "status": status,
            "reason": reason,
            "details": details or {},
        }

    async def _tool(self, tool: str, args: dict) -> dict:
        if tool == "train_worker":
            return await self._train("SCV", {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS"})
        if tool == "train_marine":
            return await self._train("MARINE", {"BARRACKS"})
        if tool == "gather":
            return await self._gather(args)
        if tool in {"build_supply", "build_refinery", "build_barracks", "build_engineering_bay", "expand"}:
            names = {
                "build_supply": "SUPPLYDEPOT",
                "build_refinery": "REFINERY",
                "build_barracks": "BARRACKS",
                "build_engineering_bay": "ENGINEERINGBAY",
                "expand": "COMMANDCENTER",
            }
            return await self._build(names[tool], args)
        if tool == "research_infantry":
            return await self._research(args)
        return await self._tactics(tool, args)

    def _definition(self, name: str) -> Any:
        from sc2.ids.unit_typeid import UnitTypeId

        definition = self.types.get(UnitTypeId[name].value)
        if definition is None:
            raise Rejected(f"Unit type {name} is unavailable in this map")
        return definition

    def _cost(self, definition: Any) -> dict:
        common = self.response.observation.player_common
        cost = {
            "minerals": int(definition.mineral_cost),
            "vespene": int(definition.vespene_cost),
            "supply": float(definition.food_required),
        }
        if common.minerals - self.reserved["minerals"] < cost["minerals"]:
            raise Rejected("Insufficient minerals after this round's reservations")
        if common.vespene - self.reserved["vespene"] < cost["vespene"]:
            raise Rejected("Insufficient vespene after this round's reservations")
        if common.food_cap - common.food_used - self.reserved["supply"] < cost["supply"]:
            raise Rejected("Insufficient supply")
        return cost

    def _reserve(self, cost: dict, tags: list[int]) -> None:
        for key, value in cost.items():
            self.reserved[key] += value
        self.busy.update(tags)

    async def _abilities(self, tag: int) -> set[int]:
        query = self.query_pb.RequestQuery(
            abilities=[self.query_pb.RequestQueryAvailableAbilities(unit_tag=tag)],
            ignore_resource_requirements=False,
        )
        reply = await self.client._execute(query=query)
        return {ability.ability_id for ability in reply.query.abilities[0].abilities}

    async def _command(self, tags: list[int], ability: int, point=None, target: int | None = None) -> dict:
        order = self.raw_pb.ActionRawUnitCommand(ability_id=ability, unit_tags=tags, queue_command=False)
        if point is not None:
            order.target_world_space_pos.x, order.target_world_space_pos.y = point
        if target is not None:
            order.target_unit_tag = target
        action = self.pb.Action(action_raw=self.raw_pb.ActionRaw(unit_command=order))
        reply = await self.client._execute(action=self.pb.RequestAction(actions=[action]))
        results = list(reply.action.result)
        if not results or any(result != 1 for result in results):
            raise Rejected(f"SC2 rejected native action: {results}")
        return {
            "ability_id": ability,
            "unit_tags": tags,
            "target": list(point) if point else target,
            "engine_results": results,
            "completion": "pending_observation",
        }

    async def _train(self, name: str, buildings: set[str]) -> dict:
        definition = self._definition(name)
        cost = self._cost(definition)
        candidates = [
            unit
            for unit in self._own()
            if self._name(unit) in buildings
            and unit.build_progress >= 1
            and not unit.orders
            and unit.tag not in self.busy
        ]
        for building in candidates:
            if self._ability_available(await self._abilities(building.tag), definition.ability_id):
                result = await self._command([building.tag], definition.ability_id)
                self._reserve(cost, [building.tag])
                return {**result, "requested_unit": name, "cost": cost}
        raise Rejected(f"No ready idle production building can currently train {name}")

    async def _gather(self, args: dict) -> dict:
        from sc2.ids.ability_id import AbilityId

        resource = args.get("resource", "minerals")
        if resource not in {"minerals", "gas", "vespene"}:
            raise Rejected("resource must be minerals or gas")
        workers = self._gather_candidates(resource)
        if "unit_tag" in args:
            workers = [unit for unit in workers if unit.tag == args["unit_tag"]]
        if not workers:
            raise Rejected("No eligible idle or opposite-resource owned SCV is available",
                           {"resource": resource, "workers_commanded": 0})
        targets = (
            [unit for unit in self.response.observation.raw_data.units if unit.mineral_contents > 0]
            if resource == "minerals"
            else [unit for unit in self._own() if self._name(unit) == "REFINERY" and unit.build_progress >= 1]
        )
        if not targets:
            raise Rejected("No visible resource target or ready refinery exists",
                           {"resource": resource, "workers_commanded": 0})
        maximum = args.get("max_workers", 12 if resource == "minerals" else 3)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 24:
            raise Rejected("max_workers must be between 1 and 24")
        capacity = self._gas_capacity() if resource != "minerals" else []
        remaining = {value["target_tag"]: value["remaining_capacity"] for value in capacity}
        if resource != "minerals" and not any(value is not None and value > 0 for value in remaining.values()):
            reason = "gas_capacity_unknown" if any(value is None for value in remaining.values()) else "gas_saturated"
            raise Rejected(reason, {"resource": resource, "workers_commanded": 0, "capacity_before": capacity})
        available_targets = targets if resource == "minerals" else [
            target for target in targets if remaining.get(int(target.tag)) is not None
            and remaining[int(target.tag)] > 0]
        gather_ability = AbilityId.HARVEST_GATHER_SCV.value
        reply = await self.client._execute(query=self.query_pb.RequestQuery(
            abilities=[self.query_pb.RequestQueryAvailableAbilities(unit_tag=worker.tag) for worker in workers],
            ignore_resource_requirements=False,
        ))
        native = {int(item.unit_tag): {int(value.ability_id) for value in item.abilities}
                  for item in reply.query.abilities}
        workers = [worker for worker in workers
                   if self._ability_available(native.get(int(worker.tag), set()), gather_ability)]
        if not workers:
            raise Rejected("No eligible owned SCV has the native gathering ability",
                           {"resource": resource, "workers_commanded": 0})

        def worker_priority(worker):
            distance = min(math.dist((target.pos.x, target.pos.y), (worker.pos.x, worker.pos.y))
                           for target in available_targets)
            return (bool(worker.orders), distance, int(worker.tag)) if resource == "minerals" else (
                distance, bool(worker.orders), int(worker.tag))

        workers.sort(key=worker_priority)
        orders = []
        partial_failure = None
        for worker in workers[:maximum]:
            available = targets if resource == "minerals" else [
                target for target in targets if remaining.get(int(target.tag)) is not None
                and remaining[int(target.tag)] > 0]
            if not available:
                break
            target = min(
                available, key=lambda unit: math.dist((unit.pos.x, unit.pos.y), (worker.pos.x, worker.pos.y))
            )
            try:
                order = await self._command([worker.tag], gather_ability, target=target.tag)
            except Rejected as exc:
                if not orders:
                    raise
                partial_failure = str(exc)
                break
            orders.append({**order, "source_resource": "idle" if not worker.orders else (
                "gas" if resource == "minerals" else "minerals"),
                "source_target_tags": sorted({int(item.target_unit_tag) for item in worker.orders})})
            self.busy.add(worker.tag)
            if resource != "minerals":
                tag = int(target.tag)
                remaining[tag] -= 1
                self.gas_reserved[tag] = self.gas_reserved.get(tag, 0) + 1
        return {"orders": orders, "resource": resource, "workers_commanded": len(orders),
                "requested_max_workers": maximum, "capacity_before": capacity,
                "partial_failure": partial_failure, "completion": "pending_observation"}

    async def _construction_workers(self, ability: int, workers: list[Any]) -> list[Any]:
        response = await self.client._execute(query=self.query_pb.RequestQuery(
            abilities=[self.query_pb.RequestQueryAvailableAbilities(unit_tag=unit.tag) for unit in workers],
            ignore_resource_requirements=False,
        ))
        abilities = {int(item.unit_tag): {int(value.ability_id) for value in item.abilities}
                     for item in response.query.abilities}
        result = [worker for worker in workers
                  if self._ability_available(abilities.get(int(worker.tag), set()), ability)]
        if not result:
            raise Rejected("No eligible SCV has the native construction ability; prerequisites or resources may be missing")
        return result

    def _build_radius(self, ability: int, fallback: float | None = None) -> float:
        """Native build-footprint geometry, or a reported geyser collision radius."""
        abilities = list(getattr(self.data, "abilities", []))
        remaps = {item.ability_id: item.remaps_to_ability_id for item in abilities if item.remaps_to_ability_id}
        canonical = remaps.get(ability, ability)
        definition = next((item for item in abilities if remaps.get(item.ability_id, item.ability_id) == canonical
                           and item.footprint_radius > 0), None)
        radius = float(definition.footprint_radius) if definition is not None else fallback
        if radius is None or not math.isfinite(radius) or not 0 < radius <= 32:
            raise Rejected("Native construction footprint unavailable; path feasibility cannot be verified")
        return radius

    async def _reachable_worker(self, workers: list[Any], center: tuple[float, float], radius: float):
        """Batch path queries to standable points outside the proposed footprint.

        A legal placement does not prove the Euclidean-nearest SCV can get there.
        Native paths select a reachable worker; they do not select a new building
        type or strategic destination. The engine remains the final authority.
        """
        jobs = []
        for worker in workers:
            offset = radius + max(float(worker.radius), 0.5) + 0.125
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)):
                point = (center[0] + dx * offset, center[1] + dy * offset)
                if 0 <= point[0] < self.width and 0 <= point[1] < self.height:
                    jobs.append((worker, point))
        reachable = []
        for first in range(0, len(jobs), 256):
            chunk = jobs[first:first + 256]
            response = await self.client._execute(query=self.query_pb.RequestQuery(pathing=[
                self.query_pb.RequestQueryPathing(
                    unit_tag=worker.tag, end_pos=self.common_pb.Point2D(x=point[0], y=point[1]))
                for worker, point in chunk
            ]))
            if len(response.query.pathing) != len(chunk):
                raise RuntimeError("Native path query returned an incomplete response")
            for (worker, point), answer in zip(chunk, response.query.pathing):
                distance = float(answer.distance)
                already_there = math.dist((worker.pos.x, worker.pos.y), point) <= 0.0001
                if math.isfinite(distance) and (distance > 0 or (distance == 0 and already_there)):
                    reachable.append((distance, bool(worker.orders), int(worker.tag), worker, point))
        if not reachable:
            return None
        distance, _busy, _tag, worker, approach = min(reachable, key=lambda item: item[:3])
        return worker, {"source": "native_pathing", "worker_tag": int(worker.tag), "placement": list(center),
                        "approach": list(approach), "distance": distance, "footprint_radius": radius,
                        "queried_paths": len(jobs)}

    async def _placement(self, ability: int, workers: list[Any], center: tuple[float, float]):
        workers = await self._construction_workers(ability, workers)
        radius = self._build_radius(ability)
        offsets = [(0, 0)] + [
            (x, y)
            for radius in (3, 6, 9, 12)
            for x, y in (
                (radius, 0),
                (-radius, 0),
                (0, radius),
                (0, -radius),
                (radius, radius),
                (-radius, radius),
                (radius, -radius),
                (-radius, -radius),
            )
        ]
        points = [(round(center[0] + x) + 0.5, round(center[1] + y) + 0.5) for x, y in offsets]
        points = [point for point in points if 0 <= point[0] < self.width and 0 <= point[1] < self.height]
        queries = [
            self.query_pb.RequestQueryBuildingPlacement(
                ability_id=ability,
                target_pos=self.common_pb.Point2D(x=x, y=y),
                placing_unit_tag=workers[0].tag,
            )
            for x, y in points
        ]
        response = await self.client._execute(query=self.query_pb.RequestQuery(placements=queries))
        if len(response.query.placements) != len(points):
            raise RuntimeError("Native placement query returned an incomplete response")
        legal = False
        for point, placement in zip(points, response.query.placements):
            if placement.result == 1:
                legal = True
                reachable = await self._reachable_worker(workers, point, radius)
                if reachable is not None:
                    worker, evidence = reachable
                    return worker, point, evidence
        if legal:
            raise Rejected("No eligible SCV can reach a legal construction approach near the requested position")
        raise Rejected("SC2 placement query found no legal site near the requested position")

    async def _build(self, name: str, args: dict) -> dict:
        definition = self._definition(name)
        cost = self._cost(definition)
        workers = self._eligible_builders()
        if not workers:
            raise Rejected("No idle or harvesting owned SCV is available; active tasks were not interrupted")
        if name == "COMMANDCENTER":
            bases = [
                unit
                for unit in self._own()
                if self._name(unit) in {"COMMANDCENTER", "ORBITALCOMMAND", "PLANETARYFORTRESS"}
            ]
            sites = [
                tuple(point)
                for point in self._expansions()
                if all(math.dist(point, (base.pos.x, base.pos.y)) > 16 for base in bases)
            ]
            if not sites:
                raise Rejected("No unoccupied known expansion candidate exists")
            default = min(sites, key=lambda point: math.dist(point, self.start_point))
        else:
            default = (
                min(self.start_point[0] + 7, self.width - 4),
                min(self.start_point[1] + 7, self.height - 4),
            )
        point = destination(args, default, self.width, self.height)
        if name == "REFINERY":
            refineries = [unit for unit in self._own() if self._name(unit) == "REFINERY"]
            geysers = [
                unit
                for unit in self.response.observation.raw_data.units
                if unit.vespene_contents > 0
                and unit.owner != self.player_id
                and all(
                    math.dist((unit.pos.x, unit.pos.y), (refinery.pos.x, refinery.pos.y)) > 2
                    for refinery in refineries
                )
            ]
            if not geysers:
                raise Rejected("No known unoccupied geyser exists")
            workers = await self._construction_workers(definition.ability_id, workers)
            reachable = None
            for geyser in sorted(geysers, key=lambda unit: math.dist((unit.pos.x, unit.pos.y), point)):
                radius = self._build_radius(definition.ability_id, float(geyser.radius) or None)
                reachable = await self._reachable_worker(workers, (geyser.pos.x, geyser.pos.y), radius)
                if reachable is not None:
                    break
            if reachable is None:
                raise Rejected("No eligible SCV can reach an unoccupied geyser construction approach")
            worker, evidence = reachable
            result = await self._command([worker.tag], definition.ability_id, target=geyser.tag)
        else:
            worker, point, evidence = await self._placement(definition.ability_id, workers, point)
            result = await self._command([worker.tag], definition.ability_id, point=point)
        self._reserve(cost, [worker.tag])
        return {**result, "requested_structure": name, "cost": cost, "pathing": evidence}

    async def _research(self, args: dict) -> dict:
        from sc2.ids.upgrade_id import UpgradeId

        upgrade_name = args.get("upgrade", "TERRANINFANTRYWEAPONSLEVEL1")
        if upgrade_name not in {"TERRANINFANTRYWEAPONSLEVEL1", "TERRANINFANTRYARMORSLEVEL1"}:
            raise Rejected("Only level-one infantry weapons or armor is supported")
        upgrade_id = UpgradeId[upgrade_name].value
        if upgrade_id in self.response.observation.raw_data.player.upgrade_ids:
            raise Rejected("Upgrade is already complete")
        if any(item["upgrade"] == upgrade_name for item in self._upgrades()["in_progress"]):
            raise Rejected("Upgrade is already in progress")
        definition = next((item for item in self.data.upgrades if item.upgrade_id == upgrade_id), None)
        if definition is None:
            raise Rejected("Upgrade is unavailable")
        common = self.response.observation.player_common
        if common.minerals - self.reserved["minerals"] < definition.mineral_cost:
            raise Rejected("Insufficient minerals")
        if common.vespene - self.reserved["vespene"] < definition.vespene_cost:
            raise Rejected("Insufficient vespene")
        for building in self._own():
            if self._name(building) != "ENGINEERINGBAY" or building.build_progress < 1 or building.orders:
                continue
            if building.tag in self.busy or not self._ability_available(
                await self._abilities(building.tag), definition.ability_id
            ):
                continue
            result = await self._command([building.tag], definition.ability_id)
            self._reserve(
                {"minerals": definition.mineral_cost, "vespene": definition.vespene_cost}, [building.tag]
            )
            return result
        raise Rejected("A ready idle Engineering Bay is required; no prerequisite is built automatically")

    async def _tactics(self, tool: str, args: dict) -> dict:
        from sc2.ids.ability_id import AbilityId

        if tool == "scout":
            candidates = self._scout_candidates()
            if "unit_tag" in args:
                candidates = [unit for unit in candidates if unit.tag == args["unit_tag"]]
            else:
                tracked = self._tracked_scout()
                if tracked is not None:
                    candidates = [unit for unit in candidates if unit.tag == tracked.tag]
                    if not candidates:
                        raise Rejected("Assigned scout is busy or constructing; no replacement was dispatched")
            if not candidates:
                raise Rejected("No owned unit is available to scout")
            tags = [candidates[0].tag]
        else:
            tags = [unit.tag for unit in self._army() if unit.tag not in self.busy]
            if not tags:
                raise Rejected("No owned combat unit is available")
        default = self.start_point if tool == "retreat" else self.enemy_start
        point = destination(args, default, self.width, self.height)
        if tool == "hold":
            result = await self._command(tags, AbilityId.HOLDPOSITION_HOLD.value)
        elif tool == "attack":
            result = await self._command(tags, AbilityId.ATTACK_ATTACK.value, point=point)
        else:
            result = await self._command(tags, AbilityId.MOVE_MOVE.value, point=point)
        if tool == "scout":
            self.scout_tag = int(tags[0])
        self.busy.update(tags)
        return result

    async def close(self) -> dict:
        if self.closed:
            return self.close_result
        self.closed = True
        replay = None
        errors = []
        try:
            if self.client is not None and self.config.get("replay", True):
                try:
                    assert self.run_dir is not None
                    path = self.run_dir / "native.SC2Replay"
                    await asyncio.wait_for(self.client.save_replay(str(path)), 6)
                    replay = {"name": path.name, "path": str(path), "size": path.stat().st_size}
                except Exception as exc:  # noqa: BLE001 - replay failure must never skip process cleanup
                    errors.append(f"replay: {type(exc).__name__}")
        finally:
            if self.process is not None:
                try:
                    await asyncio.wait_for(self.process.__aexit__(None, None, None), 8)
                except Exception as exc:  # noqa: BLE001 - SDK cleanup can fail after a crashed engine
                    errors.append(f"process_close: {type(exc).__name__}")
                    self.process._clean(verbose=False)
        self.close_result = {
            "status": "closed",
            "replay": replay,
            "engine_result": self.engine_result,
            "engine_identity": dict(self.engine_identity),
            "errors": errors,
        }
        return self.close_result


class Rejected(ValueError):
    """A valid proposal could not be accepted by the current native game state."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

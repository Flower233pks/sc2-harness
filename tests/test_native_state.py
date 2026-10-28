"""Native protocol fixtures test visibility, acknowledgments and cleanup, not strategy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from s2clientprotocol import common_pb2, data_pb2, query_pb2, raw_pb2, sc2api_pb2

from sc2_harness.native_worker import NativeSession, Rejected, destination, validate_command


def session():
    game = NativeSession()
    game.player_id = 1
    game.pb = sc2api_pb2
    game.raw_pb = raw_pb2
    game.response = sc2api_pb2.ResponseObservation()
    game.response.observation.player_common.player_id = 1
    game.width, game.height = 64, 64
    game.types = {
        45: data_pb2.UnitTypeData(unit_id=45, mineral_cost=50, food_required=1),
        21: data_pb2.UnitTypeData(unit_id=21, attributes=[8]),
    }
    return game


def test_snapshot_enemy_does_not_leak_to_model_and_unfinished_building_is_not_complete():
    game = session()
    raw = game.response.observation.raw_data
    raw.units.add(tag=1, owner=1, unit_type=45, alliance=1, display_type=1, build_progress=1)
    raw.units.add(tag=2, owner=1, unit_type=21, alliance=1, display_type=1, build_progress=0.5)
    raw.units.add(tag=3, owner=2, unit_type=45, alliance=4, display_type=2, health=45)
    raw.units.add(tag=4, owner=2, unit_type=45, alliance=4, display_type=1, health=45)
    observation = game._observation()
    assert {unit["tag"] for unit in observation["units"]} == {1, 2, 4}
    assert observation["enemy_visible"] == 1
    assert observation["structures"].get("BARRACKS", 0) == 0
    assert observation["unit_counts"]["BARRACKS"] == 1
    assert observation["units"][0]["owner"] == "self"
    assert observation["units"][0]["owner_id"] == 1
    assert observation["rendering"] == "unavailable"


def test_round_reservation_prevents_two_orders_spending_same_minerals():
    game = session()
    game.response.observation.player_common.minerals = 50
    game.response.observation.player_common.food_cap = 15
    game.response.observation.player_common.food_used = 12
    definition = game.types[45]
    cost = game._cost(definition)
    game._reserve(cost, [1])
    with pytest.raises(Rejected, match="minerals"):
        game._cost(definition)


@pytest.mark.asyncio
async def test_native_rejection_is_not_reported_as_acceptance():
    game = session()

    async def execute(**kwargs):
        assert kwargs["action"].actions[0].action_raw.unit_command.unit_tags == [7]
        return sc2api_pb2.Response(action=sc2api_pb2.ResponseAction(result=[3]))

    game.client = SimpleNamespace(_execute=execute)
    with pytest.raises(Rejected, match="SC2 rejected"):
        await game._command([7], 16, point=(10, 12))


@pytest.mark.asyncio
async def test_native_acknowledgment_does_not_claim_completion():
    game = session()

    async def execute(**_):
        return sc2api_pb2.Response(action=sc2api_pb2.ResponseAction(result=[1]))

    game.client = SimpleNamespace(_execute=execute)
    result = await game._command([7], 16, point=(10, 12))
    assert result["engine_results"] == [1]
    assert result["completion"] == "pending_observation"


@pytest.mark.asyncio
async def test_foreign_scout_tag_cannot_issue_action():
    game = session()
    game.response.observation.raw_data.units.add(
        tag=42,
        owner=2,
        unit_type=45,
        alliance=4,
        display_type=1,
        build_progress=1,
    )
    with pytest.raises(Rejected, match="No owned"):
        await game._tactics("scout", {"unit_tag": 42})


@pytest.mark.asyncio
async def test_explicit_gas_request_can_redirect_miner_but_not_active_scout():
    game = session()
    raw = game.response.observation.raw_data
    miner = raw.units.add(tag=1, owner=1, unit_type=45, build_progress=1)
    miner.orders.add(ability_id=295, target_unit_tag=10)
    scout = raw.units.add(tag=2, owner=1, unit_type=45, build_progress=1)
    scout.orders.add(ability_id=16, target_world_space_pos=common_pb2.Point(x=20, y=20))
    raw.units.add(tag=10, owner=16, unit_type=341, mineral_contents=1000)
    raw.units.add(tag=20, owner=1, unit_type=20, build_progress=1, assigned_harvesters=0, ideal_harvesters=3)
    orders = []

    async def command(tags, ability, point=None, target=None):
        orders.append((tags, target))
        return {"unit_tags": tags, "target": target}

    game._command = command
    game.query_pb = query_pb2

    async def execute(**kwargs):
        assert [item.unit_tag for item in kwargs["query"].abilities] == [1]
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        response.query.abilities.add(unit_tag=1).abilities.add(ability_id=295)
        return response

    game.client = SimpleNamespace(_execute=execute)
    await game._gather({"resource": "gas"})
    assert orders == [([1], 20)]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_worker_task", ["construction", "distant_idle_scout"])
async def test_new_build_preserves_construction_and_prefers_local_miner_over_distant_idle_scout(first_worker_task):
    game = session()
    game.response.observation.player_common.minerals = 200
    game.response.observation.player_common.food_cap = 15
    game.types[19] = data_pb2.UnitTypeData(unit_id=19, mineral_cost=100, ability_id=319)
    raw = game.response.observation.raw_data
    building_worker = raw.units.add(tag=1, owner=1, unit_type=45, build_progress=1)
    if first_worker_task == "construction":
        building_worker.pos.CopyFrom(common_pb2.Point(x=7, y=7))
        building_worker.orders.add(ability_id=319, target_world_space_pos=common_pb2.Point(x=7, y=7))
    else:
        # A scout has finished at the far enemy base. Do not summon it home
        # merely because local miners have harvesting orders.
        building_worker.pos.CopyFrom(common_pb2.Point(x=60, y=60))
    miner = raw.units.add(tag=2, owner=1, unit_type=45, build_progress=1)
    miner.pos.CopyFrom(common_pb2.Point(x=20, y=20))
    miner.orders.add(ability_id=295, target_unit_tag=10)
    orders = []

    async def abilities(_):
        return {319}

    async def placement(_ability, workers, center):
        if first_worker_task == "construction":
            assert [worker.tag for worker in workers] == [2]
        selected = next(worker for worker in workers if worker.tag == 2)
        return selected, center, {"source": "fixture_pathing"}

    async def command(tags, _ability, point):
        orders.extend(tags)
        return {"unit_tags": tags, "target": point}

    game._abilities, game._placement, game._command = abilities, placement, command
    await game._build("SUPPLYDEPOT", {})
    assert orders == [2]


@pytest.mark.asyncio
async def test_replay_failure_does_not_skip_native_cleanup(tmp_path):
    game = session()
    game.run_dir = tmp_path
    cleaned = []

    async def save_replay(_):
        raise RuntimeError("engine connection lost")

    async def exit_process(*_):
        cleaned.append(True)

    game.client = SimpleNamespace(save_replay=save_replay)
    game.process = SimpleNamespace(__aexit__=exit_process)
    result = await game.close()
    assert cleaned == [True]
    assert result["replay"] is None
    assert result["errors"] == ["replay: RuntimeError"]


def test_invalid_role_shape_and_nonfinite_destination_are_rejected():
    with pytest.raises(ValueError, match="authorized"):
        validate_command({"command_id": "x", "actor_id": [], "tool": "attack", "args": {}})
    with pytest.raises(ValueError, match="outside"):
        destination({"x": float("nan"), "y": 4}, (0, 0), 64, 64)
    with pytest.raises(ValueError, match="Unsupported"):
        validate_command({"command_id": "x", "actor_id": "tactics", "tool": "hold", "args": {"x": 1}})


def test_raw_rgb_payload_is_native_and_bad_payload_fails(tmp_path):
    game = session()
    game.run_dir = tmp_path
    frame = game.response.observation.render_data.map
    frame.size.CopyFrom(common_pb2.Size2DI(x=2, y=2))
    frame.bits_per_pixel = 24
    frame.data = bytes([255, 0, 0] * 4)
    game.response.observation.game_loop = 8
    game._capture_frame()
    assert game.frame_path.is_file()
    assert game.frame_path.read_bytes().startswith(b"\xff\xd8")
    assert game._observation()["rendering"] == "rgb"
    frame.data = b"truncated"
    with pytest.raises(RuntimeError, match="truncated"):
        game._capture_frame()


@pytest.mark.asyncio
async def test_game_creation_embeds_map_bytes_for_case_sensitive_linux_install(tmp_path, monkeypatch):
    """Official Maps/ paths must not be resolved via the engine's lowercase maps/."""
    from sc2 import maps, sc2process

    from sc2_harness import native_worker

    map_path = tmp_path / "Maps" / "Melee" / "Simple64.SC2Map"
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"fixture-map-data-not-an-installed-game")
    requests = []
    cleaned = []

    async def execute(**kwargs):
        requests.append(kwargs["create_game"])
        raise RuntimeError("request captured before launching a game")

    async def ping():
        return sc2api_pb2.Response(ping=sc2api_pb2.ResponsePing(game_version="4.10.0", base_build=75689))

    class ProcessFixture:
        def __init__(self, **_):
            self._process = SimpleNamespace(pid=123)

        async def __aenter__(self):
            return SimpleNamespace(ping=ping, _execute=execute)

    async def close(_):
        cleaned.append(True)

    monkeypatch.setattr(native_worker, "probe", lambda: {"available": True})
    monkeypatch.setattr(maps, "get", lambda _: SimpleNamespace(path=map_path, data=map_path.read_bytes()))
    monkeypatch.setattr(sc2process, "SC2Process", ProcessFixture)
    monkeypatch.setattr(NativeSession, "_configure_renderer", lambda _: False)
    monkeypatch.setattr(NativeSession, "close", close)
    with pytest.raises(RuntimeError, match="request captured"):
        await NativeSession().start({"map_name": "Simple64"}, str(tmp_path / "run"))
    assert cleaned == [True]
    assert requests[0].local_map.map_path == str(map_path)
    assert requests[0].local_map.map_data == map_path.read_bytes()
    assert [player.type for player in requests[0].player_setup] == [1, 2]
    assert requests[0].realtime is False

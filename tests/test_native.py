"""Native observation contracts use protocol fixtures, without starting SC2."""

from types import SimpleNamespace

import pytest
from s2clientprotocol import common_pb2, data_pb2, query_pb2, raw_pb2, sc2api_pb2

from sc2_harness.native_worker import NativeSession, Rejected


def game_fixture():
    game = NativeSession()
    game.player_id = 1
    game.pb, game.raw_pb = sc2api_pb2, raw_pb2
    game.query_pb, game.common_pb = query_pb2, common_pb2
    game.response = sc2api_pb2.ResponseObservation()
    game.width = game.height = 100
    game.start_point, game.enemy_start = (10, 10), (80, 80)
    common = game.response.observation.player_common
    common.player_id, common.minerals, common.vespene = 1, 1000, 200
    common.food_cap, common.food_used = 50, 14
    game.types = {
        identity: data_pb2.UnitTypeData(unit_id=identity, ability_id=ability, mineral_cost=cost,
                                        food_required=food, attributes=[8] if structure else [])
        for identity, ability, cost, food, structure in (
            (18, 318, 400, 0, True), (19, 319, 100, 0, True), (20, 320, 75, 0, True),
            (21, 321, 150, 0, True), (22, 322, 125, 0, True),
            (45, 524, 50, 1, False), (48, 560, 50, 1, False),
        )
    }
    game.data = SimpleNamespace(abilities=[data_pb2.AbilityData(ability_id=ability, footprint_radius=radius)
                                          for ability, radius in [(318, 2.5), (319, 1), (320, 1.5), (321, 1.5), (322, 1.5)]],
                                upgrades=[data_pb2.UpgradeData(
        upgrade_id=7, ability_id=652, mineral_cost=100, vespene_cost=100)])
    return game


def unit(game, tag, unit_type, progress=1):
    value = game.response.observation.raw_data.units.add(
        tag=tag, unit_type=unit_type, owner=1, alliance=1, display_type=1, build_progress=progress)
    value.pos.x, value.pos.y = 10, 10
    return value


def test_training_queue_does_not_count_as_completed_unit_or_pending_building():
    game = game_fixture()
    barracks = unit(game, 1, 21)
    barracks.orders.add(ability_id=560, progress=0.5)
    unit(game, 2, 48)
    unit(game, 3, 48)
    unit(game, 4, 19, 0.4)
    observation = game._observation()
    assert observation["ready_counts"]["MARINE"] == 2
    assert observation["queued_counts"] == {"MARINE": 1}
    assert observation["pending_counts"] == {"SUPPLYDEPOT": 1}
    assert "SUPPLYDEPOT" not in observation["structures"]
    assert observation["production"][0]["queue_length"] == 1
    assert observation["production"][0]["is_busy"] is True
    assert observation["production"][0]["orders"][0]["progress"] == 0.5


def test_upgrade_order_is_in_progress_and_only_native_upgrade_id_means_complete():
    game = game_fixture()
    bay = unit(game, 1, 22)
    bay.orders.add(ability_id=652, progress=0.25)
    assert game._observation()["upgrades"] == {"completed": [], "in_progress": [{
        "upgrade": "TERRANINFANTRYWEAPONSLEVEL1", "unit_tag": 1, "ability_id": 652, "progress": 0.25}]}
    bay.ClearField("orders")
    game.response.observation.raw_data.player.upgrade_ids.append(7)
    assert game._observation()["upgrades"] == {
        "completed": ["TERRANINFANTRYWEAPONSLEVEL1"], "in_progress": []}


def test_absent_native_score_is_unknown_but_explicit_zero_and_loss_values_are_preserved():
    game = game_fixture()
    score = game._observation()["score"]
    assert score["available"] is False
    assert score["collected_vespene"] is None
    assert score["lost_minerals"]["army"] is None
    native = game.response.observation.score
    native.score_type = 2
    native.score_details.collected_vespene = 0
    native.score_details.lost_minerals.army = 50
    score = game._observation()["score"]
    assert score["available"] is True
    assert score["score_type"] == "Melee"
    assert score["collected_vespene"] == 0
    assert score["lost_minerals"]["army"] == 50
    assert score["lost_minerals"]["economy"] is None


@pytest.mark.asyncio
async def test_affordances_use_one_native_batch_and_do_not_advertise_busy_producer_or_builder():
    game = game_fixture()
    builder = unit(game, 1, 45)
    builder.orders.add(ability_id=319)
    miner = unit(game, 2, 45)
    miner.orders.add(ability_id=295, target_unit_tag=700)
    unit(game, 3, 18)
    barracks = unit(game, 4, 21)
    barracks.orders.add(ability_id=560, progress=0.5)
    unit(game, 5, 48)
    unit(game, 6, 22)
    refinery = unit(game, 7, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 0, 3
    unit(game, 8, 19, 0.4)
    neutral = game.response.observation.raw_data.units
    neutral.add(tag=700, unit_type=341, owner=16, mineral_contents=1000, pos=common_pb2.Point(x=50, y=50))
    neutral.add(tag=701, unit_type=342, owner=16, vespene_contents=1000, pos=common_pb2.Point(x=20, y=20))
    calls = []
    native_abilities = {1: [319, 321], 2: [16, 295, 318, 319, 320, 321, 322],
                        3: [524], 4: [560], 5: [16, 18, 23], 6: [652], 7: []}

    async def execute(**kwargs):
        request = kwargs["query"]
        calls.append(request)
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        for request_unit in request.abilities:
            answer = response.query.abilities.add(unit_tag=request_unit.unit_tag)
            for ability in native_abilities[request_unit.unit_tag]:
                answer.abilities.add(ability_id=ability)
        return response

    game.client = SimpleNamespace(_execute=execute)
    await game._refresh_action_state()
    assert len(calls) == 1
    assert {item.unit_tag for item in calls[0].abilities} == {1, 2, 3, 4, 5, 6, 7}
    assert calls[0].ignore_resource_requirements is False
    state = game._observation()["action_state"]
    assert state["build_supply"]["unit_tags"] == [2]
    assert state["build_barracks"]["available"] is True
    assert state["train_marine"]["available"] is False
    assert state["attack"]["unit_tags"] == [5]
    assert state["research_infantry"]["available"] is True
    assert state["gather"]["resources"] == ["gas"]
    workers = game._observation()["worker_state"]
    assert workers["constructing_tags"] == [1]
    assert workers["eligible_builder_tags"] == [2]


@pytest.mark.asyncio
async def test_missing_native_ability_reply_is_not_guessed_available():
    game = game_fixture()
    unit(game, 1, 45)

    async def execute(**_):
        return sc2api_pb2.Response(query=query_pb2.ResponseQuery())

    game.client = SimpleNamespace(_execute=execute)
    await game._refresh_action_state()
    assert not any(value["available"] for value in game.action_state.values())


@pytest.mark.asyncio
async def test_combat_commands_only_target_marines_and_cannot_replace_barracks_training_order():
    game = game_fixture()
    barracks = unit(game, 1, 21)
    barracks.orders.add(ability_id=560, progress=0.5)
    unit(game, 2, 48)
    unit(game, 3, 48)
    captured = []

    async def command(tags, ability, **_):
        captured.append((tags, ability))
        return {"unit_tags": tags}

    game._command = command
    for tool in ["hold", "attack"]:
        game.busy.clear()
        await game._tactics(tool, {})
    assert captured == [([2, 3], 18), ([2, 3], 23)]
    assert barracks.orders[0].ability_id == 560
    assert game._observation()["queued_counts"] == {"MARINE": 1}


@pytest.mark.asyncio
async def test_second_engineering_bay_cannot_start_duplicate_observed_upgrade():
    game = game_fixture()
    bay = unit(game, 1, 22)
    bay.orders.add(ability_id=652, progress=0.25)
    unit(game, 2, 22)
    with pytest.raises(Rejected, match="already in progress"):
        await game._research({})


def add_miners(game, count=5):
    game.response.observation.raw_data.units.add(
        tag=700, unit_type=341, owner=16, alliance=3, display_type=1, mineral_contents=1000)
    for identity in range(10, 10 + count):
        worker = unit(game, identity, 45)
        worker.orders.add(ability_id=295, target_unit_tag=700)


def gather_query_client(game, abilities=None):
    """Protocol-level ability/ACK fixture; no inferred capability in the executor."""
    calls = []

    async def execute(**kwargs):
        calls.append(kwargs)
        if "action" in kwargs:
            return sc2api_pb2.Response(action=sc2api_pb2.ResponseAction(result=[1]))
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        for requested in kwargs["query"].abilities:
            answer = response.query.abilities.add(unit_tag=requested.unit_tag)
            for ability in ([295] if abilities is None else abilities.get(requested.unit_tag, [])):
                answer.abilities.add(ability_id=ability)
        return response

    game.client = SimpleNamespace(_execute=execute)
    return calls


@pytest.mark.asyncio
async def test_gas_assignment_reserves_native_capacity_within_the_turn():
    game = game_fixture()
    add_miners(game)
    gather_query_client(game)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 2, 3
    for tag in (80, 81):
        unit(game, tag, 45).orders.add(ability_id=295, target_unit_tag=100)
    orders = []

    async def command(tags, ability, target):
        orders.append((tags[0], target))
        return {"unit_tags": tags, "target": target}

    game._command = command
    result = await game._gather({"resource": "gas", "max_workers": 10})
    assert result["workers_commanded"] == 1
    assert len(orders) == 1
    assert game.gas_reserved == {100: 1}
    with pytest.raises(Rejected, match="gas_saturated") as rejected:
        await game._gather({"resource": "gas", "max_workers": 10})
    assert rejected.value.details["workers_commanded"] == 0
    assert len(orders) == 1


@pytest.mark.asyncio
async def test_gas_enroute_orders_count_toward_capacity_even_before_native_assigned_updates():
    game = game_fixture()
    add_miners(game)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 0, 3
    for tag in (80, 81, 82):
        unit(game, tag, 45).orders.add(ability_id=295, target_unit_tag=100)
    with pytest.raises(Rejected, match="gas_saturated"):
        await game._gather({"resource": "gas", "max_workers": 10})
    state = game._observation()["worker_state"]
    assert state["gas_worker_count"] == 3
    assert state["gas_assignments"][0]["remaining_capacity"] == 0

    async def execute(**kwargs):
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        for requested in kwargs["query"].abilities:
            response.query.abilities.add(unit_tag=requested.unit_tag).abilities.add(ability_id=295)
        return response

    game.client = SimpleNamespace(_execute=execute)
    await game._refresh_action_state()
    assert game.action_state["gather"]["available"] is True
    assert game.action_state["gather"]["resources"] == ["minerals"]
    assert game.action_state["gather"]["unit_tags"] == [80, 81, 82]


@pytest.mark.asyncio
async def test_gas_assignment_spreads_only_to_native_spare_slots_across_refineries():
    game = game_fixture()
    add_miners(game)
    gather_query_client(game)
    for tag, assigned, position in ((100, 2, 10), (101, 1, 20)):
        refinery = unit(game, tag, 20)
        refinery.assigned_harvesters, refinery.ideal_harvesters = assigned, 3
        refinery.pos.x = position
    targets = []

    async def command(tags, ability, target):
        targets.append(target)
        return {"unit_tags": tags, "target": target}

    game._command = command
    result = await game._gather({"resource": "gas", "max_workers": 10})
    assert result["workers_commanded"] == 3
    assert targets.count(100) == 1
    assert targets.count(101) == 2


@pytest.mark.asyncio
async def test_unknown_gas_capacity_never_guesses_three_workers():
    game = game_fixture()
    add_miners(game)
    unit(game, 100, 20)
    with pytest.raises(Rejected, match="gas_capacity_unknown") as rejected:
        await game._gather({"resource": "gas", "max_workers": 10})
    assert rejected.value.details["workers_commanded"] == 0
    assert rejected.value.details["capacity_before"][0]["remaining_capacity"] is None


@pytest.mark.asyncio
async def test_gas_assignment_uses_nearby_miner_instead_of_recalling_distant_idle_scout():
    game = game_fixture()
    add_miners(game, 1)
    gather_query_client(game)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 2, 3
    scout = unit(game, 90, 45)
    scout.pos.x, scout.pos.y = 80, 80
    commanded = []

    async def command(tags, ability, target):
        commanded.extend(tags)
        return {"unit_tags": tags, "target": target}

    game._command = command
    await game._gather({"resource": "gas", "max_workers": 1})
    assert commanded == [10]


def test_harvester_projection_preserves_unknown_and_does_not_infer_returning_worker_resource():
    game = game_fixture()
    add_miners(game, 1)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 3, 3
    unit(game, 80, 45).orders.add(ability_id=295, target_unit_tag=100)
    unit(game, 81, 45).orders.add(ability_id=296, target_unit_tag=200)
    unit(game, 200, 18)
    observation = game._observation()
    units = {value["tag"]: value for value in observation["units"]}
    assert units[100]["assigned_harvesters"] == 3
    assert units[200]["assigned_harvesters"] is None
    assert observation["worker_state"]["mineral_worker_count"] == 1
    assert observation["worker_state"]["gas_worker_count"] == 1
    assert observation["worker_state"]["unknown_harvest_worker_count"] == 1
    assert observation["worker_state"]["gas_assignments"][0]["remaining_capacity"] == 0


def test_native_neutral_snapshot_positions_are_resource_candidates_but_visible_empty_minerals_are_not():
    game = game_fixture()
    game.types[341] = data_pb2.UnitTypeData(unit_id=341, has_minerals=True)
    raw = game.response.observation.raw_data.units
    raw.add(tag=701, unit_type=341, owner=16, alliance=3, display_type=1,
            mineral_contents=100, pos=common_pb2.Point(x=10, y=10))
    raw.add(tag=702, unit_type=341, owner=16, alliance=3, display_type=2,
            mineral_contents=0, pos=common_pb2.Point(x=80, y=80))
    raw.add(tag=703, unit_type=341, owner=16, alliance=3, display_type=1,
            mineral_contents=0, pos=common_pb2.Point(x=80, y=10))
    assert game._expansions() == [[10, 10], [80, 80]]
    diagnostics = {value["tag"]: value for value in game._resource_diagnostics()["resources"]}
    assert diagnostics[702]["mineral_contents"] == 0
    assert diagnostics[702]["mineral_contents_known"] is False
    assert diagnostics[702]["depleted"] is False
    assert diagnostics[703]["depleted"] is True


@pytest.mark.asyncio
async def test_partially_accepted_gather_keeps_actual_command_count_and_failure_evidence():
    game = game_fixture()
    add_miners(game)
    gather_query_client(game)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 0, 3
    calls = []

    async def command(tags, ability, target):
        calls.append(tags)
        if len(calls) == 2:
            raise Rejected("native rejected second worker")
        return {"unit_tags": tags, "target": target}

    game._command = command
    result = await game._gather({"resource": "gas", "max_workers": 3})
    assert result["workers_commanded"] == 1
    assert result["partial_failure"] == "native rejected second worker"
    assert game.gas_reserved == {100: 1}


@pytest.mark.asyncio
async def test_explicit_mineral_gather_uses_idle_first_then_own_gas_workers_with_limits_and_provenance():
    game = game_fixture()
    add_miners(game, 0)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 3, 3
    idle = unit(game, 1, 45)
    idle.pos.x = 80
    for tag in (2, 3):
        unit(game, tag, 45).orders.add(ability_id=295, target_unit_tag=100)
    calls = gather_query_client(game)
    await game._refresh_action_state()
    assert game.action_state["gather"]["resources"] == ["minerals"]
    assert game.action_state["gather"]["unit_tags"] == [1, 2, 3]

    result = await game._gather({"resource": "minerals", "max_workers": 2})
    assert result["workers_commanded"] == result["requested_max_workers"] == 2
    assert [order["unit_tags"] for order in result["orders"]] == [[1], [2]]
    assert [(order["source_resource"], order["source_target_tags"]) for order in result["orders"]] == [
        ("idle", []), ("gas", [100])]
    assert all(order["target"] == 700 and order["ability_id"] == 295 for order in result["orders"])
    assert all(order["engine_results"] == [1] for order in result["orders"])
    assert result["completion"] == "pending_observation"
    assert game.busy == {1, 2}
    # The unchanged observation cannot cause the same SCV to be reassigned twice in one turn.
    next_result = await game._gather({})
    assert [order["unit_tags"] for order in next_result["orders"]] == [[3]]
    assert len([call for call in calls if "action" in call]) == 3


@pytest.mark.asyncio
async def test_mineral_transfer_protects_construction_movement_other_resources_unknown_origins_and_scouts():
    game = game_fixture()
    add_miners(game, 0)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 3, 3
    enemy_refinery = unit(game, 101, 20)
    enemy_refinery.owner, enemy_refinery.alliance = 2, 4
    unit(game, 102, 20, 0.5)
    unit(game, 103, 18)
    for tag, ability, target in (
        (1, 295, 100), (2, 295, 700), (3, 319, 0), (4, 16, 0),
        (5, 295, 101), (6, 295, 102), (7, 296, 103), (8, 295, 100),
    ):
        unit(game, tag, 45).orders.add(ability_id=ability, target_unit_tag=target)
    unit(game, 9, 45)
    game.scout_tag = 9
    gather_query_client(game)
    await game._refresh_action_state()
    assert game.action_state["gather"]["resources"] == ["minerals"]
    assert game.action_state["gather"]["unit_tags"] == [1, 8]
    game.busy.add(8)  # A different command reserved this worker after the decision observation.
    result = await game._gather({"resource": "minerals", "max_workers": 24})
    assert [order["unit_tags"] for order in result["orders"]] == [[1]]
    assert result["orders"][0]["source_target_tags"] == [100]


@pytest.mark.asyncio
@pytest.mark.parametrize("native_available", [False, True])
async def test_mineral_transfer_requires_native_gather_ability_in_affordance_and_execution(native_available):
    game = game_fixture()
    add_miners(game, 0)
    refinery = unit(game, 100, 20)
    refinery.assigned_harvesters, refinery.ideal_harvesters = 3, 3
    unit(game, 1, 45).orders.add(ability_id=295, target_unit_tag=100)
    calls = gather_query_client(game, {1: [295] if native_available else []})
    await game._refresh_action_state()
    assert game.action_state["gather"]["available"] is native_available
    assert game.action_state["gather"]["resources"] == (["minerals"] if native_available else [])
    if native_available:
        assert (await game._gather({}))["workers_commanded"] == 1
    else:
        with pytest.raises(Rejected, match="native gathering ability"):
            await game._gather({})
        assert not any("action" in call for call in calls)
    executor_queries = [call["query"] for call in calls[1:] if "query" in call]
    assert len(executor_queries) == 1
    assert [request.unit_tag for request in executor_queries[0].abilities] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [0, 25])
async def test_mineral_transfer_rejects_out_of_range_max_workers_without_native_orders(maximum):
    game = game_fixture()
    add_miners(game, 0)
    unit(game, 100, 20)
    unit(game, 1, 45).orders.add(ability_id=295, target_unit_tag=100)
    calls = gather_query_client(game)
    with pytest.raises(Rejected, match="max_workers must be between"):
        await game._gather({"resource": "minerals", "max_workers": maximum})
    assert calls == []


@pytest.mark.asyncio
async def test_scout_reuses_its_moving_unit_for_default_and_explicit_updates():
    game = game_fixture()
    scout = unit(game, 1, 45)
    unit(game, 2, 45)
    commanded = []

    async def command(tags, ability, **kwargs):
        commanded.append((tags, kwargs["point"]))
        return {"unit_tags": tags}

    game._command = command
    await game._tactics("scout", {})
    assert game.scout_tag == 1
    scout.orders.add(ability_id=16, target_world_space_pos=common_pb2.Point(x=80, y=80))
    game.busy.clear()
    await game._tactics("scout", {"x": 70, "y": 70})
    game.busy.clear()
    await game._tactics("scout", {"unit_tag": 1, "x": 60, "y": 60})
    assert commanded == [([1], (80, 80)), ([1], (70, 70)), ([1], (60, 60))]
    assert game.scout_tag == 1


@pytest.mark.asyncio
async def test_default_scout_is_reselected_only_after_owned_unit_disappears():
    game = game_fixture()
    scout = unit(game, 1, 45)
    unit(game, 2, 45)
    commanded = []

    async def command(tags, ability, **kwargs):
        commanded.extend(tags)
        return {"unit_tags": tags}

    game._command = command
    await game._tactics("scout", {})
    game.response.observation.raw_data.units.remove(scout)
    game.busy.clear()
    await game._tactics("scout", {})
    assert commanded == [1, 2]
    assert game.scout_tag == 2


@pytest.mark.asyncio
async def test_constructing_default_scout_is_not_silently_replaced_but_explicit_legal_reassignment_is_allowed():
    game = game_fixture()
    constructing = unit(game, 1, 45)
    constructing.orders.add(ability_id=319)
    unit(game, 2, 45)
    moving = unit(game, 3, 45)
    moving.orders.add(ability_id=16, target_world_space_pos=common_pb2.Point(x=20, y=20))
    game.scout_tag = 1

    async def execute(**kwargs):
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        for requested in kwargs["query"].abilities:
            response.query.abilities.add(unit_tag=requested.unit_tag).abilities.add(ability_id=16)
        return response

    async def command(tags, ability, **kwargs):
        return {"unit_tags": tags}

    game.client, game._command = SimpleNamespace(_execute=execute), command
    await game._refresh_action_state()
    state = game.action_state["scout"]
    assert state["unit_tags"] == [2]
    assert state["available"] is True
    assert state["default_unit_tag"] == 1
    assert state["default_available"] is False
    with pytest.raises(Rejected, match="no replacement was dispatched"):
        await game._tactics("scout", {})
    with pytest.raises(Rejected, match="No owned unit"):
        await game._tactics("scout", {"unit_tag": 3})
    await game._tactics("scout", {"unit_tag": 2})
    assert game.scout_tag == 2


@pytest.mark.asyncio
async def test_moving_tracked_scout_remains_in_native_affordance_candidates():
    game = game_fixture()
    scout = unit(game, 1, 45)
    scout.orders.add(ability_id=16, target_world_space_pos=common_pb2.Point(x=80, y=80))
    unit(game, 2, 45)
    game.scout_tag = 1

    async def execute(**kwargs):
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        for requested in kwargs["query"].abilities:
            response.query.abilities.add(unit_tag=requested.unit_tag).abilities.add(ability_id=16)
        return response

    game.client = SimpleNamespace(_execute=execute)
    await game._refresh_action_state()
    assert game.action_state["scout"]["unit_tags"] == [1, 2]
    assert game.action_state["scout"]["default_unit_tag"] == 1
    assert game.action_state["scout"]["default_available"] is True


def construction_query_client(game, path_distance, placement_valid=None):
    calls = []

    async def execute(**kwargs):
        request = kwargs["query"]
        calls.append(request)
        response = sc2api_pb2.Response(query=query_pb2.ResponseQuery())
        for requested in request.abilities:
            answer = response.query.abilities.add(unit_tag=requested.unit_tag)
            for ability in (318, 319, 320, 321, 322):
                answer.abilities.add(ability_id=ability)
        for index, requested in enumerate(request.placements):
            legal = placement_valid(requested, index) if placement_valid else index == 0
            response.query.placements.add(result=1 if legal else 44)
        for requested in request.pathing:
            response.query.pathing.add(distance=path_distance(requested))
        return response

    game.client = SimpleNamespace(_execute=execute)
    return calls


@pytest.mark.asyncio
async def test_construction_uses_reachable_worker_instead_of_euclidean_nearest_trapped_scv():
    game = game_fixture()
    near = unit(game, 1, 45)
    near.pos.x, near.pos.y = 11, 11
    far = unit(game, 2, 45)
    far.pos.x, far.pos.y = 30, 30
    calls = construction_query_client(game, lambda request: 0 if request.unit_tag == 1 else 20)
    actions = []

    async def command(tags, ability, **kwargs):
        actions.append((tags, ability, kwargs))
        return {"unit_tags": tags, "ability_id": ability, "engine_results": [1]}

    game._command = command
    result = await game._build("SUPPLYDEPOT", {"x": 10, "y": 10})
    assert actions[0][0] == [2]
    assert result["pathing"]["worker_tag"] == 2
    assert result["pathing"]["distance"] == 20
    path_batches = [request for request in calls if request.pathing]
    assert len(path_batches) == 1
    assert {item.unit_tag for item in path_batches[0].pathing} == {1, 2}
    assert len(path_batches[0].pathing) > 2


@pytest.mark.asyncio
async def test_construction_rejects_when_no_worker_can_reach_a_legal_site():
    game = game_fixture()
    worker = unit(game, 1, 45)
    worker.pos.x, worker.pos.y = 50, 50
    construction_query_client(game, lambda _: 0)
    with pytest.raises(Rejected, match="No eligible SCV can reach"):
        await game._build("SUPPLYDEPOT", {"x": 10, "y": 10})
    assert game.busy == set()
    assert game.reserved["minerals"] == 0


@pytest.mark.asyncio
async def test_construction_can_choose_a_reachable_legal_nearby_site_after_first_site_is_unreachable():
    game = game_fixture()
    worker = unit(game, 1, 45)
    worker.pos.x, worker.pos.y = 50, 50
    construction_query_client(
        game, lambda request: 10 if request.end_pos.x >= 14 else 0,
        lambda request, _index: request.target_pos.x in {10.5, 13.5} and request.target_pos.y == 10.5)

    async def command(tags, ability, **kwargs):
        return {"unit_tags": tags, "ability_id": ability, "engine_results": [1], "target": kwargs["point"]}

    game._command = command
    result = await game._build("SUPPLYDEPOT", {"x": 10, "y": 10})
    assert result["target"] == (13.5, 10.5)
    assert result["pathing"]["approach"][0] >= 14


@pytest.mark.asyncio
async def test_refinery_queries_reachable_geyser_edges_not_blocked_geyser_center():
    game = game_fixture()
    unit(game, 1, 45)
    unit(game, 2, 45)
    geyser = game.response.observation.raw_data.units.add(
        tag=700, unit_type=342, owner=16, alliance=3, display_type=1, vespene_contents=2000, radius=1.5)
    geyser.pos.x, geyser.pos.y = 20, 20
    calls = construction_query_client(game, lambda request: 0 if request.unit_tag == 1 else 15)

    async def command(tags, ability, **kwargs):
        return {"unit_tags": tags, "ability_id": ability, "engine_results": [1], "target": kwargs["target"]}

    game._command = command
    result = await game._build("REFINERY", {})
    assert result["unit_tags"] == [2]
    assert result["target"] == 700
    endpoints = [item.end_pos for request in calls for item in request.pathing]
    assert endpoints
    assert all(max(abs(point.x - 20), abs(point.y - 20)) > 1.5 for point in endpoints)


def receipt_fixture(game, command_id, tags, ability):
    return game._receipt({"command_id": command_id, "actor_id": "production", "tool": "build_barracks"},
                         "accepted", "SC2 accepted native action",
                         {"unit_tags": tags, "ability_id": ability, "engine_results": [1],
                          "completion": "pending_observation"})


@pytest.mark.parametrize("native_ability", [321, 1000])
def test_late_native_failure_preserves_ack_and_command_identity_with_native_ability_remap(native_ability):
    game = game_fixture()
    game.data.abilities.append(data_pb2.AbilityData(ability_id=321, remaps_to_ability_id=1000))
    receipt = receipt_fixture(game, "one", [7], 321)
    game._associate_action_errors([receipt], [{"unit_tag": 7, "ability_id": native_ability, "result": 208}])
    assert receipt["status"] == "accepted"
    assert receipt["details"]["engine_results"] == [1]
    assert receipt["details"]["execution_status"] == "failed"
    assert receipt["details"]["native_errors"][0]["result_name"] == "CouldntReachTarget"
    assert receipt["details"]["native_errors"][0]["command_id"] == "one"
    assert receipt["details"]["native_errors"][0]["ability_id"] == native_ability
    assert receipt["details"]["observed_action_errors"] == receipt["details"]["native_errors"]
    assert game.unmatched_action_errors == []


def test_same_worker_different_ability_error_is_not_attributed_to_current_command():
    game = game_fixture()
    receipt = receipt_fixture(game, "one", [7], 321)
    game._associate_action_errors([receipt], [{"unit_tag": 7, "ability_id": 322, "result": 208}])
    assert receipt["details"]["execution_status"] == "pending_observation"
    assert "native_errors" not in receipt["details"]
    assert game._observation()["unmatched_action_errors"][0]["association_reason"] == "no_command_match"


def test_ambiguous_command_pair_is_left_unmatched_instead_of_attached_to_both():
    game = game_fixture()
    receipts = [receipt_fixture(game, "one", [7], 321), receipt_fixture(game, "two", [7], 321)]
    game._associate_action_errors(receipts, [{"unit_tag": 7, "ability_id": 321, "result": 208}])
    assert all(receipt["details"]["execution_status"] == "pending_observation" for receipt in receipts)
    assert len(game.unmatched_action_errors) == 1
    assert game.unmatched_action_errors[0]["association_reason"] == "ambiguous_command_match"
    assert "command_id" not in game.unmatched_action_errors[0]


def test_multi_unit_command_is_partial_when_only_one_unit_has_a_native_error():
    game = game_fixture()
    receipt = receipt_fixture(game, "one", [7, 8], 23)
    game._associate_action_errors([receipt], [{"unit_tag": 7, "ability_id": 23, "result": 208}])
    assert receipt["details"]["execution_status"] == "partial"
    assert receipt["details"]["failed_unit_tags"] == [7]
    assert receipt["details"]["pending_unit_tags"] == [8]

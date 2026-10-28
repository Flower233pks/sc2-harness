import asyncio
import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from sc2_harness import policies
from sc2_harness.policies import ROLES, LLMPolicy, PolicyError, ScriptedPolicy, validate_decision


def late_tactics_observation(*, broadcast_tags=24, with_details=False):
    """Synthetic native-shaped late input, not native-game evidence."""
    order = {"ability_id": 23, "ability": "ATTACK_ATTACK", "progress": 0.5,
             "target_unit_tag": None, "target": [38.5, 122.5], "produces": None, "researches": None}
    units = [
        {"tag": index + 1, "type": "MARINE", "owner": "self", "is_structure": False,
         "is_ready": True, "is_idle": False, "x": 35 + index, "y": 120,
         "health": 3 if index == 0 else 45, "health_max": 45, "shield": 0,
         "orders": [deepcopy(order) for _ in range(4)]}
        for index in range(24)
    ]
    units.append({"tag": 9001, "type": "SUPPLYDEPOTLOWERED", "owner": "enemy", "visible": True,
                  "is_structure": True, "is_ready": True, "x": 38, "y": 134,
                  "health": 400, "health_max": 400, "shield": 0})
    observation = {
        "engine": "sc2", "player_id": 1, "game_time": 1030, "army_count": 24,
        "minerals": 650, "vespene": 320, "supply_used": 29, "supply_cap": 47,
        "ready_counts": {"MARINE": 24, "BARRACKS": 4, "COMMANDCENTER": 1},
        "pending_counts": {}, "queued_counts": {"MARINE": 2},
        "structures": {"BARRACKS": 4, "COMMANDCENTER": 1}, "unit_counts": {"MARINE": 24},
        "map": {"width": 160, "height": 160, "enemy_start": [38.5, 122.5]},
        "task": {"scenario": "full_match", "objective": "Win the native game",
                 "role_phase": "native_victory", "continuous_role_duty": True,
                 "observed_at_game_time": 1030,
                 "role_milestones": [{"metric": "native_victory", "target": 1, "done": False}],
                 "role_objective": "Continue coordinating the army until observed native Victory."},
        "units": units,
        "action_state": {
            tool: {"available": True, "unit_tags": list(range(1, broadcast_tags + 1))}
            for tool in ("attack", "retreat", "hold")
        },
        "last_receipts": [{"command_id": "failed-attack", "actor_id": "tactics", "tool": "attack",
                           "status": "accepted", "details": {"execution_status": "failed",
                           "target": [38.5, 122.5], "native_errors": [
                               {"command_id": "failed-attack", "unit_tag": 1, "ability_id": 23,
                                "result": 1, "result_name": "Error", "game_loop": 23000}]}}],
    }
    observation["action_state"]["scout"] = {
        "available": True, "unit_tags": [24], "default_unit_tag": 24, "default_available": True}
    if with_details:
        observation["production"] = [
            {"tag": 100 + index, "type": "BARRACKS", "is_ready": True,
             "is_busy": True, "queue_length": 4, "orders": [deepcopy(order) for _ in range(4)]}
            for index in range(24)
        ]
        observation["worker_state"] = {
            "idle_tags": list(range(100, 112)), "gas_worker_count": 3,
            "gas_assignments": [{"target_tag": index, "remaining_capacity": 0} for index in range(24)]}
    return observation


def late_economy_production_observation(role):
    """Synthetic late native-shaped input with four off-role Marine samples."""
    order = {"ability_id": 560, "ability": "BARRACKSTRAIN_MARINE", "progress": 0.5,
             "target_unit_tag": None, "target": None, "produces": "MARINE", "researches": None}
    structures = [
        {"tag": 1000 + index, "type": "BARRACKS", "owner": "self", "is_structure": True,
         "is_ready": True, "is_idle": False, "x": index + 20, "y": 10,
         "health": 1000, "health_max": 1000, "shield": 0, "orders": [deepcopy(order)]}
        for index in range(32)
    ]
    workers = [{"tag": 4000000000 + index, "type": "SCV", "owner": "self", "is_structure": False,
                "is_ready": True, "x": index, "y": 5, "orders": []} for index in range(48)]
    marines = [{"tag": 2000 + index, "type": "MARINE", "owner": "self", "is_structure": False,
                "is_ready": True, "x": index, "y": 30, "health": 45, "health_max": 45,
                "orders": [deepcopy(order) for _ in range(4)]} for index in range(4)]
    return {
        "engine": "sc2", "player_id": 1, "game_time": 730, "army_count": 4, "workers": 48,
        "minerals": 300, "vespene": 120, "ready_counts": {"BARRACKS": 32, "SCV": 48, "MARINE": 4},
        "pending_counts": {}, "queued_counts": {"MARINE": 32},
        "units": structures + workers + marines + [
            {"tag": 5000, "type": "FACTORY", "owner": "enemy", "visible": True,
             "is_structure": True, "x": 83, "y": 96, "health": 900, "health_max": 1250}],
        "production": [{"tag": unit["tag"], "type": "BARRACKS", "is_ready": True,
                        "is_busy": True, "queue_length": 1, "orders": [deepcopy(order)]}
                       for unit in structures[:24]],
        "worker_state": {"idle_tags": [unit["tag"] for unit in workers[:12]],
                         "gas_worker_count": 3, "mineral_worker_count": 45},
        "task": {"scenario": "full_match", "objective": "Win the native game",
                 "role_objective": "Continue the current given execution duty. " * 85,
                 "reference_plan": {"profile": "marine_reference_v1", "advisory": True,
                                    "phase": "recommended_assault_phase", "focus": "Continue the role execution."}},
        "action_state": {tool: {"available": True, "unit_tags": [unit["tag"] for unit in workers]}
                         for tool in policies.ROLE_TOOLS[role]},
    }


def decision(tool="train_worker", args=None):
    return {
        "goal": "Develop the base",
        "reason": "Resources are available.",
        "actions": [{"tool": tool, "args": args or {}}],
        "messages": [],
    }


def response(payload, **extra):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, **extra}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        },
    )


@pytest.mark.parametrize("role", ["economy", "production"])
def test_lifecycle_examples_are_optional_valid_decisions_and_explain_funded_parallel_work(role):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        example = policies._LIFECYCLE_EXAMPLES[role]
        assert validate_decision(role, example["possible_decision"])["actions"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide(role, {"task": {"scenario": "full_match"}}, [])
            prompt = requests[0]["messages"][0]["content"]
            assert "starting one NEW request" in prompt
            assert "not an unpaid remainder" in prompt and "Construction still depends on its builder" in prompt
            assert "not a required policy" in prompt and "not the current observation" in prompt
            assert out["actions"] == []  # Examples do not take over the model's decision.
            if role == "economy":
                assert "extra gas does not speed it up" in prompt and "zero gas" in prompt
            else:
                assert "independent jobs" in prompt and "Ready idle Barracks" in prompt
            assert policies._TARGET_EXAMPLE["possible_decision"]["actions"][0]["args"] != {"x": 38, "y": 134}

    asyncio.run(run())


@pytest.mark.parametrize("chosen_actions", [[], [{"tool": "attack", "args": {"x": 38, "y": 134}}]])
def test_current_visible_target_and_full_army_health_are_grounded_without_choosing_the_models_action(chosen_actions):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": chosen_actions})

        observation = late_tactics_observation()
        observation["units"] = observation["units"][:3] + observation["units"][-1:]
        observation["army_count"] = 3
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("tactics", observation, [])
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        targets = current["combat_status"]["visible_enemy_targets"]["structures"]
        assert len(targets) == 1 and targets[0]["x"] == 38 and targets[0]["y"] == 134
        assert targets[0]["health"] == targets[0]["health_max"] == 400
        health = current["combat_status"]["army_health"]
        assert health["ready_units"] == 3 and health["full_health_units"] == 2 and health["damaged_units"] == 1
        assert health["coverage_complete"] is True
        prompt = requests[0]["messages"][0]["content"]
        assert "\"enemy_start\":[8,9]" in prompt and "\"x\":21,\"y\":47" in prompt
        assert "(38,134)" not in prompt and "exact position" in prompt
        assert "400/400" in prompt and "Full health is not observed health loss" in prompt
        assert "not a required policy" in prompt
        assert out["actions"] == chosen_actions and out["request_count"] == 1
        assert observation == original

    asyncio.run(run())


def test_late_payload_deduplicates_large_broadcast_tags_but_retains_scout_failures_and_evidence():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("scout", {"unit_tag": 24, "x": 38, "y": 134}))

        observation = late_tactics_observation(broadcast_tags=1500)
        observation["task"]["role_objective"] = "Coordinate until native Victory. " * 100
        original = deepcopy(observation)
        initial = policies._compact_observation(observation, "tactics")
        assert len(policies._current_payload_content({"observation": initial, "messages": []})) > 24000
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("tactics", observation, [])
        content = requests[0]["messages"][-1]["content"]
        assert len(content) <= 24000 and out["request_count"] == 1
        current = json.loads(content)["observation"]
        assert current["input_projection"]["budget"]["steps"][0] == "deduplicate_counts_and_broadcast_tags"
        assert "attack" not in current["action_context"]["eligible_unit_tags"]
        assert current["action_context"]["scout_state"]["default_unit_tag"] == 24
        assert current["task"] == observation["task"]
        assert current["action_context"]["recent_execution_failures"][0]["ack_status"] == "accepted"
        assert current["last_receipts"][0]["details"]["execution_status"] == "failed"
        assert current["combat_status"]["visible_enemy_targets"]["structures"][0]["health_max"] == 400
        assert current["ready_counts"] == observation["ready_counts"]
        assert observation == original and initial == policies._compact_observation(observation, "tactics")

    asyncio.run(run())


def test_late_tactics_payload_uses_finite_role_and_army_sample_reductions_without_dropping_current_targets():
    observation = late_tactics_observation(with_details=True)
    # Legitimate multiple queued orders make this synthetic current input exceed
    # the real 24K guard even after economy/producer detail has been removed.
    for unit in observation["units"][:24]:
        for order in unit["orders"]:
            order["ability"] = "ATTACK_ATTACK_POSITION_OR_UNIT_WITH_QUEUED_MOVEMENT"
    original = deepcopy(observation)
    compact = policies._compact_observation(observation, "tactics")
    payload = policies._bounded_current_payload(compact, "tactics", [], guided=True)
    current = json.loads(payload)["observation"]
    steps = current["input_projection"]["budget"]["steps"]
    assert len(payload) <= 24000 and len(steps) <= 5
    assert "omit_tactics_worker_and_producer_details" in steps
    assert any(step.startswith("limit_own_combat_samples_") for step in steps)
    assert "worker_state" not in current and "production" not in current
    assert current["input_projection"]["own_combat_sample_omitted"] > 0
    assert any(unit["tag"] == 24 for unit in current["units"])  # Tracked scout remains represented.
    assert any(unit["tag"] == 9001 for unit in current["units"])
    assert current["task"] == observation["task"]
    assert current["combat_status"]["army_health"]["ready_units"] == 24
    assert current["combat_status"]["visible_enemy_targets"]["structures"][0]["x"] == 38
    assert current["action_context"]["recent_execution_failures"]
    assert observation == original


def test_budget_receipts_keep_ack_and_failure_summary_while_bounding_repeated_order_samples():
    observation = late_tactics_observation()
    receipt = observation["last_receipts"][0]
    receipt["details"]["orders"] = [
        {"unit_tag": index, "ability": "ATTACK_ATTACK", "target": [38.5, 122.5]} for index in range(1000)]
    receipt["details"]["unit_tags"] = list(range(1000))
    original = deepcopy(observation)
    compact = policies._compact_observation(observation, "tactics")
    payload = policies._bounded_current_payload(compact, "tactics", [], guided=True)
    assert len(payload) <= 24000
    current = json.loads(payload)["observation"]
    bounded = current["last_receipts"][0]
    assert bounded["status"] == "accepted" and bounded["details"]["execution_status"] == "failed"
    assert len(bounded["details"]["orders"]) == 4
    assert bounded["details"]["omitted_detail_items"]["orders"] == 996
    assert current["action_context"]["recent_execution_failures"][0]["command_id"] == "failed-attack"
    assert observation == original


@pytest.mark.parametrize("unit_type,tag", [("MARINE", 25), ("SCV", 105)])
def test_tracked_scout_outside_initial_army_or_worker_sample_keeps_position_and_orders_after_budgeting(unit_type, tag):
    observation = late_tactics_observation(broadcast_tags=1500, with_details=True)
    observation["task"]["role_objective"] = "Coordinate until native Victory. " * 100
    for worker in range(5):
        observation["units"].append({"tag": 101 + worker, "type": "SCV", "owner": "self",
                                     "is_structure": False, "is_ready": True, "x": worker, "y": 5})
    tracked = {"tag": tag, "type": unit_type, "owner": "self", "is_structure": False,
               "is_ready": True, "x": 73, "y": 91,
               "orders": [{"ability": "MOVE_MOVE", "target": [38, 134]}]}
    observation["units"] = [unit for unit in observation["units"] if unit["tag"] != tag] + [tracked]
    observation["action_state"]["scout"] = {
        "available": True, "default_available": True, "default_unit_tag": tag, "unit_tags": [tag]}
    original = deepcopy(observation)
    compact = policies._compact_observation(observation, "tactics")
    content = policies._bounded_current_payload(compact, "tactics", [], guided=True)
    current = json.loads(content)["observation"]
    assert len(content) <= 24000 and "budget" in current["input_projection"]
    sample = next(unit for unit in current["units"] if unit["tag"] == tag)
    assert sample["x"] == 73 and sample["y"] == 91
    assert sample["orders"][0] == {"ability": "MOVE_MOVE", "target": [38, 134]}
    assert current["action_context"]["scout_state"]["default_unit_tag"] == tag
    assert observation == original


@pytest.mark.parametrize("display_type", [2, 3, 4])
def test_explicit_hidden_enemy_is_not_leaked_through_mixed_units_or_legacy_enemy_list(display_type):
    observation = late_tactics_observation()
    hidden = {"tag": 8000, "owner": "enemy", "type": "HIDDEN_BUNKER",
              "visible": True, "display_type": display_type, "is_structure": True, "x": 19, "y": 23}
    observation["units"].append(hidden)
    observation["enemy_visible"] = [deepcopy(hidden)]
    compact = policies._compact_observation(observation, "tactics")
    assert "HIDDEN_BUNKER" not in policies._current_payload_content(compact)
    assert compact["combat_status"]["visible_enemy_targets"]["structures"][0]["tag"] == 9001


@pytest.mark.parametrize("guided", [True, False])
def test_irreducible_current_task_still_fails_24k_guard_before_any_model_call(guided):
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            return response(decision())

        observation = {"guidance_enabled": guided, "task": {"objective": "X" * 25000}}
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("tactics", observation, [])
            assert caught.value.code == "input_too_large" and caught.value.request_count == 0
            assert requests == [] and policy.accounting["tactics"]["calls"] == 0
            assert observation == original

    asyncio.run(run())


def test_small_and_legacy_payloads_are_not_silently_reprojected():
    for guided, observation in [(True, {}), (False, late_tactics_observation(broadcast_tags=1500))]:
        compact = policies._compact_observation(observation, "tactics")
        expected = policies._current_payload_content({"observation": compact, "messages": []})
        assert policies._bounded_current_payload(compact, "tactics", [], guided=guided) == expected


@pytest.mark.parametrize("observed,actions", [(1, [{"tool": "attack", "args": {}}]), (12, [])])
def test_given_reference_plan_is_preserved_as_execution_guidance_without_an_attack_gate_or_autopolicy(observed, actions):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": actions})

        reference = {"profile": "marine_reference_v1", "version": 1, "advisory": True,
                     "phase": "recommended_assault_phase" if observed >= 12 else "form_reference_force",
                     "observed_at_game_time": 210, "focus": "Coordinate the given reference force using current facts",
                     "soft_targets": [{"id": "ready_marines", "role": "production", "target": 12,
                                       "observed": observed, "known": True, "met": observed >= 12}],
                     "scope": "reference_only_not_success_or_issued_actions"}
        task = {"scenario": "full_match", "objective": "Win the native game", "guidance_profile": "marine_reference_v1",
                "reference_plan": reference, "continuous_role_duty": True}
        observation = {"engine": "sc2", "task": task, "army_count": observed,
                       "action_state": {"attack": {"available": True, "unit_tags": [1]}}}
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("tactics", observation, [])
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        assert current["task"] == task and observation == original
        prompt = requests[0]["messages"][0]["content"]
        assert "explicitly GIVEN advisory plan" in prompt and "not a strategy you autonomously invented" in prompt
        assert prompt.startswith("GIVEN EXECUTION FOCUS for tactics: " + reference["focus"])
        assert "Prioritize this role-specific focus and phase" in prompt
        assert "given execution assignment" in prompt and "not indefinite idle waiting" in prompt
        assert "Only observed native Victory wins" in prompt
        assert "There is no prescribed build order or force size" not in prompt
        assert "there is no prescribed force size or mandatory attack" not in prompt
        variants = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["actions"]["items"]["oneOf"]
        assert [variant["properties"]["tool"]["const"] for variant in variants] == ["attack"]
        assert out["actions"] == actions  # Below-target attacks and above-target waits are still model decisions.

    asyncio.run(run())


def test_default_and_legacy_prompts_do_not_invent_a_given_reference_profile():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            base = {"scenario": "full_match", "objective": "Win"}
            await policy.decide("tactics", {"task": base}, [])
            default_prompt = requests[-1]["messages"][0]["content"]
            await policy.decide("tactics", {"task": {**base, "guidance_profile": "none"}}, [])
            assert requests[-1]["messages"][0]["content"] == default_prompt
            assert "There is no prescribed build order or force size" in default_prompt
            assert "explicitly GIVEN" not in default_prompt
            await policy.decide("tactics", {"guidance_enabled": False, "task": {
                **base, "guidance_profile": "marine_reference_v1", "reference_plan": {"advisory": True}}}, [])
            current = json.loads(requests[-1]["messages"][-1]["content"])["observation"]
            assert "reference_plan" not in current["task"] and "guidance_profile" not in current["task"]
            assert "explicitly GIVEN" not in requests[-1]["messages"][0]["content"]

    asyncio.run(run())


@pytest.mark.parametrize("role,digest", [
    ("economy", "c3c60463b22a98a637abe00fa02f2d68bffeb63a5e0232ae666916fd6d468ea9"),
    ("production", "828b5165b3577a58f92530d7427ea0ccb17f658059b6beb8754f43fb38a1099b"),
    ("tactics", "9fdb23973076360b5c6b311bf911947486fdcc7e628e697929d3f55e5435bdde"),
])
def test_reference_prompt_is_separate_short_focus_first_and_none_remains_byte_identical(role, digest):
    policy = object.__new__(LLMPolicy)
    context = policies.action_context(role, {})
    task = {"scenario": "full_match", "guidance_profile": "none", "continuous_role_duty": True}
    original = policy._system_prompt(role, context, task=task)
    assert hashlib.sha256(original.encode()).hexdigest() == digest
    focus = f"Distinct current {role} execution assignment from the recorded reference plan."
    plan = {"profile": "marine_reference_v1", "advisory": True, "focus": focus,
            "phase": "recommended_assault_phase"}
    concise = policy._system_prompt(role, context, task={**task, "guidance_profile": "marine_reference_v1",
                                                        "reference_plan": plan})
    assert concise.startswith(f"GIVEN EXECUTION FOCUS for {role}: {focus}")
    assert concise.index(focus) < concise.index("Team owners")
    assert len(concise) < len(original) * 0.55
    assert "Illustration only" not in concise and "One possible illustration" not in concise
    assert "recommended_assault_phase" in concise and "not role completion" in concise
    assert "Only observed native Victory wins" in concise
    assert "no prescribed force size" not in concise and "prefer actions=[] and release" not in concise
    assert "actions=[] preserves ongoing orders; hold cancels them" in concise
    assert "recent_execution_failures" in concise and "pending construction and queued units are not ready" in concise
    if role == "economy":
        assert "population supply for requested Marine production" in concise
        assert "over optional expansion" in concise and "already-funded research" in concise
    elif role == "tactics":
        assert "does not require fresh enemy vision" in concise
        assert "enemy_start is only an initial search hint" in concise
    # Missing a genuine advisory reference does not activate the new branch.
    assert policy._system_prompt(role, context, task={**task, "reference_plan": None}) == original


@pytest.mark.parametrize("actions", [[], [{"tool": "hold", "args": {}}],
                                     [{"tool": "attack", "args": {"x": 64, "y": 87}}]])
def test_reference_prompt_fronts_current_entity_coordinate_health_options_without_selecting_a_target(actions):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": actions})

        observation = {
            "engine": "sc2", "player_id": 1, "map": {"width": 160, "height": 160,
                                                        "enemy_start": [9, 11], "expansions": [[90, 100]]},
            "task": {"scenario": "full_match", "reference_plan": {
                "profile": "marine_reference_v1", "advisory": True, "phase": "recommended_assault_phase",
                "focus": "Continue the given assault execution using current observed entities."}},
            "units": [{"tag": 1, "type": "MARINE", "owner": "self", "is_ready": True,
                       "is_structure": False, "x": 40, "y": 41, "health": 45, "health_max": 45},
                      {"tag": 201, "type": "COMMANDCENTER", "owner": "enemy", "visible": True,
                       "is_structure": True, "x": 81, "y": 95, "health": 333, "health_max": 1500},
                      {"tag": 202, "type": "FACTORY", "owner": "enemy", "visible": True,
                       "is_structure": True, "x": 64, "y": 87, "health": 1200, "health_max": 1250}],
            "action_state": {tool: {"available": True, "unit_tags": [1]} for tool in ("attack", "hold")},
        }
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("tactics", observation, [])
        prompt = requests[0]["messages"][0]["content"]
        front = prompt.split("CURRENT OBSERVED ENEMY STRUCTURES (", 1)[1].split(": ", 1)[1].split(
            ". You choose the target", 1)[0]
        targets = json.loads(front)
        assert targets[0] == {"tag": 201, "type": "COMMANDCENTER", "x": 81, "y": 95,
                              "health": 333, "health_max": 1500}
        assert targets[1]["x"] == 64 and targets[1]["y"] == 87
        assert prompt.index("Reference phase:") < prompt.index(front) < prompt.index("Team owners")
        assert "never attach its health to a prior location" in prompt
        assert "map.expansions" in prompt and "You choose where" in prompt
        assert '"x":9' not in front and '"y":11' not in front
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        assert current["map"]["enemy_start"] == [9, 11]  # The search prior is not hidden.
        assert out["actions"] == actions  # Holding/waiting/choosing the second target remain model choices.
        assert observation == original

    asyncio.run(run())


def test_reference_front_target_list_is_bounded_and_does_not_reuse_previous_or_hidden_enemy_targets():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        task = {"scenario": "full_match", "reference_plan": {
            "profile": "marine_reference_v1", "advisory": True, "focus": "Choose the current execution target.",
            "phase": "recommended_assault_phase"}}
        old = {"tag": 11, "type": "OLD_VISIBLE_BUILDING", "owner": "enemy", "visible": True,
               "is_structure": True, "x": 13, "y": 17, "health": 100}
        current_units = [{"tag": 100 + i, "type": "CURRENT_BUILDING", "owner": "enemy", "visible": True,
                          "is_structure": True, "x": 50 + i, "y": 80 + i, "health": 200 + i,
                          "health_max": 1000} for i in range(10)]
        hidden = {"tag": 999, "type": "HIDDEN_SNAPSHOT", "owner": "enemy", "display_type": 2,
                  "is_structure": True, "x": 19, "y": 23, "health": 600}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("tactics", {"engine": "sc2", "task": task, "units": [old]}, [])
            await policy.decide("tactics", {"engine": "sc2", "task": task, "units": current_units + [hidden]}, [])
        prompt = requests[-1]["messages"][0]["content"]
        front = prompt.split("CURRENT OBSERVED ENEMY STRUCTURES (", 1)[1].split(": ", 1)[1].split(
            ". You choose the target", 1)[0]
        targets = json.loads(front)
        assert len(targets) == 8 and [target["tag"] for target in targets] == list(range(100, 108))
        assert "OLD_VISIBLE_BUILDING" not in prompt and "HIDDEN_SNAPSHOT" not in prompt
        current = json.loads(requests[-1]["messages"][-1]["content"])["observation"]
        assert current["combat_status"]["visible_enemy_targets"]["omitted"]["structures"] == 2
        assert "not whole-map vision" in prompt

    asyncio.run(run())


@pytest.mark.parametrize("role", ["economy", "production"])
def test_oversized_economy_production_input_omits_only_off_role_samples_and_reaches_one_actual_mock_http(role):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        observation = late_economy_production_observation(role)
        observation["task"]["role_objective"] = "Continue the current given execution duty. " * 45
        original = deepcopy(observation)
        initial = policies._compact_observation(observation, role)
        assert len(policies._current_payload_content({"observation": initial, "messages": []})) > 24000
        expected_units = [unit for unit in initial["units"] if unit["type"] != "MARINE"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide(role, observation, [])
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        assert out["request_count"] == 1 and len(requests) == 1
        assert len(requests[0]["messages"][-1]["content"]) <= 24000
        assert current["units"] == expected_units
        assert current["input_projection"]["off_role_combat_samples_omitted"] == 4
        assert current["input_projection"]["unit_samples"] == len(expected_units)
        assert current["input_projection"]["omitted_units"] == initial["input_projection"]["omitted_units"] + 4
        assert "count_parameter_free_tool_candidates" not in current["input_projection"]["budget"]["steps"]
        for key in ("task", "worker_state", "production", "ready_counts", "pending_counts", "queued_counts"):
            assert current[key] == initial[key]
        assert current["combat_status"]["army_health"] == initial["combat_status"]["army_health"]
        assert current["combat_status"]["visible_enemy_targets"] == initial["combat_status"]["visible_enemy_targets"]
        assert current["action_context"] == initial["action_context"]
        expected_schema = policies._decision_schema(role, initial["action_context"]["allowed_tools"],
                                                   initial["action_context"]["gather_resources"],
                                                   initial["action_context"]["scout_state"])
        assert requests[0]["response_format"]["json_schema"]["schema"] == expected_schema
        assert observation == original and initial == policies._compact_observation(observation, role)

    asyncio.run(run())


@pytest.mark.parametrize("role", ["economy", "production"])
def test_still_oversized_role_payload_counts_parameter_free_candidates_but_preserves_unknown_and_scout(role):
    observation = late_economy_production_observation(role)
    compact = policies._compact_observation(observation, role)
    # Unknown classification and ownership must not be treated as disposable army samples.
    unknown = [
        {"tag": 8001, "owner": "self", "type": "MARINE"},
        {"tag": 8002, "owner": "self", "is_structure": False},
        {"tag": 8003, "owner": None, "type": "MARINE", "is_structure": False},
        {"tag": 8004, "owner": "enemy", "type": "MARINE", "is_structure": False},
        {"tag": 8005, "owner": "self", "type": "UNKNOWN", "is_structure": False},
    ]
    compact["units"].extend(unknown)
    compact["input_projection"]["visible_units_total"] += len(unknown)
    compact["input_projection"]["unit_samples"] += len(unknown)
    eligible = compact["action_context"]["eligible_unit_tags"]
    eligible.update({"scout": [9001, 9002], "future_tool": [9003], "train_worker": None})
    # A known empty array has a known zero; null candidates have unknown cardinality.
    eligible["build_supply"] = []
    original = deepcopy(compact)
    content = policies._bounded_current_payload(compact, role, [], guided=True)
    current = json.loads(content)["observation"]
    assert len(content) <= 24000
    assert current["input_projection"]["budget"]["steps"][-2:] == [
        "omit_off_role_combat_samples", "count_parameter_free_tool_candidates"]
    assert all(unit in current["units"] for unit in unknown)
    context = current["action_context"]
    assert context["eligible_unit_tags"] == {"scout": [9001, 9002], "future_tool": [9003], "train_worker": None}
    assert context["eligible_unit_counts"]["build_supply"] == 0
    assert "train_worker" not in context["eligible_unit_counts"]
    for tool, tags in eligible.items():
        if tool not in {"scout", "future_tool", "train_worker"}:
            assert context["eligible_unit_counts"][tool] == len(tags)
    assert context["eligible_unit_counts_scope"] == "provided_projected_tag_arrays_not_native_totals"
    for key in ("allowed_tools", "blocked_tools", "availability_known", "recent_rejections",
                "recent_execution_failures", "scout_state"):
        assert context[key] == original["action_context"][key]
    assert current["task"] == compact["task"]
    assert current["worker_state"] == compact["worker_state"]
    assert current["production"] == compact["production"]
    assert current["combat_status"]["visible_enemy_targets"] == compact["combat_status"]["visible_enemy_targets"]
    assert compact == original


def test_candidate_arrays_are_not_disposable_if_the_known_schema_can_take_unit_tags_or_open_arguments(monkeypatch):
    assert policies._tool_has_no_unit_tag_argument("train_worker") is True
    assert policies._tool_has_no_unit_tag_argument("gather") is True
    assert policies._tool_has_no_unit_tag_argument("scout") is False
    assert policies._tool_has_no_unit_tag_argument("future_tool") is False
    original = policies._decision_schema
    for args in [{"type": "object", "additionalProperties": False, "properties": {"unit_tag": {"type": "integer"}}},
                 {"type": "object", "properties": {}}]:
        def schema(*values, argument_schema=args):
            result = original(*values)
            result["properties"]["actions"]["items"]["oneOf"][0]["properties"]["args"] = argument_schema
            return result
        monkeypatch.setattr(policies, "_decision_schema", schema)
        assert policies._tool_has_no_unit_tag_argument("train_worker") is False


@pytest.mark.parametrize("role", ["economy", "production", "tactics"])
def test_reference_stage_header_fronts_only_own_progress_and_tools_without_turning_setup_met_into_completion(role):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        targets = [
            {"id": "ready_supply_depots", "role": "economy", "tool": "build_supply", "target": 1,
             "observed": 1, "known": True, "met": True, "gap": 0,
             "in_progress": {"types": ["SUPPLYDEPOT"], "pending": 0, "queued": 0}},
            {"id": "barracks", "role": "production", "tool": "build_barracks", "target": 1,
             "observed": 0, "known": True, "met": False, "gap": 1,
             "in_progress": {"types": ["BARRACKS"], "pending": 1, "queued": 0}},
        ]
        own = [item for item in targets if item["role"] == role]
        progress = {"assigned_targets": len(own), "met_targets": int(role == "economy"),
                    "unmet_targets": int(role == "production"), "unknown_targets": 0, "observed_at_game_time": 95}
        reference = {"profile": "marine_reference_v1", "version": 1, "revision": 2, "advisory": True,
                     "phase": "reference_opening", "role": role, "soft_targets": targets,
                     "role_targets": targets,  # Defense: the prompt must still scope these by actual actor.
                     "role_progress": progress, "focus": f"Current {role} stage assignment."}
        observation = {"task": {"scenario": "full_match", "role_phase": "native_victory",
                                "reference_plan": reference}, "minerals": 90,
                       "action_state": {"build_barracks": {"available": False, "reason": "insufficient_minerals"},
                                        "build_engineering_bay": {"available": True}}}
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide(role, observation, [])
        prompt = requests[0]["messages"][0]["content"]
        header = json.loads(prompt.split("YOUR OWN ROLE PROGRESS: ", 1)[1].split("\n", 1)[0])
        assert header == {"continuous_duty": None, "progress": progress, "targets": own, "omitted_targets": 0}
        assert prompt.index("Reference phase: reference_opening") < prompt.index("YOUR OWN ROLE PROGRESS") < prompt.index("Team owners")
        assert f"You own ONLY: {', '.join(policies.ROLE_TOOLS[role])}" in prompt
        assert "different building cannot substitute" in prompt
        assert "[] can preserve orders and leave shared minerals" in prompt
        assert "not role completion" in prompt and "Only observed native Victory wins" in prompt
        assert out["actions"] == [] and out["request_count"] == 1
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        assert current["task"]["reference_plan"] == reference and observation == original
        if role == "production":
            assert header["targets"][0]["observed"] == 0 and header["targets"][0]["in_progress"]["pending"] == 1
            assert current["action_context"]["blocked_tools"]["build_barracks"] == "insufficient_minerals"
            variants = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["actions"]["items"]["oneOf"]
            assert [item["properties"]["tool"]["const"] for item in variants] == ["build_engineering_bay"]

    asyncio.run(run())


@pytest.mark.parametrize("guided,reference,changed,reset", [
    (True, True, True, True), (True, True, False, False),
    (False, True, True, False), (True, False, True, False),
])
def test_reference_stage_history_reset_is_audited_and_does_not_change_none_or_unguided_history(
    guided, reference, changed, reset
):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "goal": "OLDER-STAGE-GOAL" if len(requests) == 1 else "Current goal", "actions": []})

        def observation(phase):
            return {"guidance_enabled": guided, "task": {"scenario": "full_match", "role_phase": "native_victory",
                    "reference_plan": {"profile": "marine_reference_v1" if reference else "none",
                                       "advisory": reference, "version": 1, "revision": 2, "phase": phase,
                                       "focus": "Follow current own-role stage progress."}}}

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            first = await policy.decide("economy", observation("reference_opening"), [])
            second_phase = "form_reference_force" if changed else "reference_opening"
            second = await policy.decide("economy", observation(second_phase), [])
            assert second["context_reset"]["reset"] is reset
            assert second["context_reset"]["discarded_messages"] == (2 if reset else 0)
            assert ("OLDER-STAGE-GOAL" in str(requests[-1]["messages"])) is not reset
            assert first["request_count"] == second["request_count"] == 1
            assert policy.states["economy"].turns == 2
            if reset:
                assert second["context_reset"]["from"]["reference_stage"] == {
                    "profile": "marine_reference_v1", "version": 1, "revision": 2, "phase": "reference_opening"}
                assert second["context_reset"]["to"]["reference_stage"]["phase"] == "form_reference_force"
            elif not reference and guided:
                assert "reference_stage" not in second["context_reset"]["to"]

    asyncio.run(run())


@pytest.mark.parametrize("args", [{"unit_tag": 7}, {"unit_tag": 7, "x": 31, "y": 67}])
def test_reference_mobility_defaults_are_explicit_without_silently_filling_or_rewriting_model_coordinates(args):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("scout", args))

        observation = {"task": {"scenario": "full_match", "reference_plan": {
            "profile": "marine_reference_v1", "advisory": True, "revision": 2, "phase": "reference_opening",
            "focus": "No mandatory early scout; choose your own useful legal command."}},
            "map": {"width": 100, "height": 100, "enemy_start": [81, 89]},
            "action_state": {"scout": {"available": True, "unit_tags": [7], "default_unit_tag": 7,
                                       "default_available": True}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("tactics", observation, [])
        prompt = requests[0]["messages"][0]["content"]
        assert "scout without x,y defaults to enemy_start even with unit_tag" in prompt
        assert "another destination requires paired x,y" in prompt
        assert "attack without x,y also defaults to enemy_start; retreat defaults home" in prompt
        assert out["actions"] == [{"tool": "scout", "args": args}]

    asyncio.run(run())


@pytest.mark.parametrize("actions", [[{"tool": "train_marine", "args": {}}], []])
def test_reference_met_production_setup_keeps_continuous_duty_and_idle_barracks_facts_but_model_selects_actions(actions):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": actions})

        duty = {"owner": "production", "activity": "train_affordable_marines_from_idle_barracks",
                "continues_after_setup": True, "until": "native_terminal", "progress": {
                    "ready_marines": 13, "queued_marines": 0, "idle_ready_barracks": 4,
                    "train_marine_available": True}}
        reference = {"profile": "marine_reference_v1", "advisory": True, "revision": 3,
                     "phase": "recommended_assault_phase", "focus": "Keep supporting the current assault.",
                     "continuous_duty": duty,
                     "role_targets": [{"id": "ready_marines", "role": "production", "tool": "train_marine",
                                       "target": 12, "observed": 13, "known": True, "met": True}],
                     "role_progress": {"assigned_targets": 1, "met_targets": 1, "unmet_targets": 0}}
        observation = {"engine": "sc2", "minerals": 2100, "ready_counts": {"BARRACKS": 4, "MARINE": 13},
                       "queued_counts": {}, "pending_counts": {"COMMANDCENTER": 1},
                       "production": [{"tag": tag, "type": "BARRACKS", "is_ready": True, "is_busy": False,
                                       "queue_length": 0, "orders": []} for tag in range(1, 5)],
                       "task": {"scenario": "full_match", "reference_plan": reference},
                       "action_state": {"train_marine": {"available": True, "unit_tags": [1, 2, 3, 4]}}}
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("production", observation, [])
        prompt = requests[0]["messages"][0]["content"]
        header = json.loads(prompt.split("YOUR OWN ROLE PROGRESS: ", 1)[1].split("\n", 1)[0])
        assert header["continuous_duty"] == duty and header["targets"][0]["met"] is True
        assert prompt.index('"continuous_duty"') < prompt.index("Team owners")
        assert "Met setup counts are not a production ceiling" in prompt
        assert "continue affordable Marine training" in prompt
        assert "does not occupy Barracks or subtract its old price" in prompt
        assert "Met setup targets do not switch off continuous_duty" in prompt
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        assert current["minerals"] == 2100 and len(current["production"]) == 4
        assert all(building["queue_length"] == 0 for building in current["production"])
        assert current["task"]["reference_plan"] == reference
        assert out["actions"] == actions  # No automatic training, forced nonempty action or target-count mask.
        assert observation == original

    asyncio.run(run())


@pytest.mark.parametrize("visited,known", [(False, True), (True, True), (False, False)])
def test_reference_tactics_distinguishes_main_army_hint_progress_from_empty_vision_and_single_scout(visited, known):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        progress = {"hint_position": [83, 97] if known else None, "radius": 15,
                    "army_ready_total": 13, "known_total": 13 if known else 0,
                    "near_count": 0 if known else None, "coverage": 1 if known else 0,
                    "positions_complete": known, "majority_near": False if known else None,
                    "initial_hint_reached": visited, "reached_at_game_time": 90 if visited else None,
                    "observed_at_game_time": 120,
                    "scope": "advisory_public_main_army_search_not_formal_goal"}
        reference = {"profile": "marine_reference_v1", "advisory": True, "revision": 3,
                     "phase": "recommended_assault_phase", "focus": "Advance the reference main army.",
                     "continuous_duty": {"owner": "tactics", "continues_after_setup": True,
                                         "until": "native_terminal", "progress": {"initial_hint_reached": visited}},
                     "initial_hint_public_progress": progress, "role_targets": []}
        observation = {"engine": "sc2", "units": [], "army_count": 13,
                       "map": {"width": 120, "height": 120, "start": [10, 10],
                               "enemy_start": [83, 97], "expansions": [[31, 67]]},
                       "task": {"scenario": "full_match", "reference_plan": reference},
                       "action_state": {"attack": {"available": True, "unit_tags": [1, 2]}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await LLMPolicy("http://localhost/v1", "test", client=client).decide("tactics", observation, [])
        prompt = requests[0]["messages"][0]["content"]
        actual_progress = json.loads(prompt.split("INITIAL HINT PUBLIC PROGRESS: ", 1)[1].split("\n", 1)[0])
        assert actual_progress == progress
        assert "No visible enemy does not mean that hint was searched" in prompt
        assert "first advance/search it with the army" in prompt
        assert "One scout is not main-army advance" in prompt
        assert "After a confirmed main-army visit without native Victory" in prompt
        assert "continue MAIN-ARMY search/advance" in prompt
        assert "combat duty continues" in prompt
        assert "If no current target is visible, or the army reaches" not in prompt
        assert out["actions"] == []  # Public guidance does not execute or force an attack.
        current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
        assert current["task"]["reference_plan"]["initial_hint_public_progress"] == progress
        assert current["map"]["enemy_start"] == [83, 97] and current["map"]["expansions"] == [[31, 67]]

    asyncio.run(run())


@pytest.mark.parametrize("role", ["economy", "production"])
def test_new_role_budget_steps_do_not_touch_already_fitting_or_unguided_content(role):
    for compact, guided in [({"units": [{"tag": 1, "type": "MARINE", "owner": "self", "is_structure": False}]}, True),
                            (policies._compact_observation(late_economy_production_observation(role), role), False)]:
        original = policies._current_payload_content({"observation": compact, "messages": []})
        assert policies._bounded_current_payload(compact, role, [], guided=guided) == original


@pytest.mark.parametrize("role", ["economy", "production"])
def test_irreducible_role_task_still_triggers_zero_http_input_guard(role):
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            return response(decision())

        observation = late_economy_production_observation(role)
        observation["task"]["objective"] = "X" * 25000
        original = deepcopy(observation)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PolicyError) as caught:
                await LLMPolicy("http://localhost/v1", "test", client=client).decide(role, observation, [])
            assert caught.value.code == "input_too_large" and caught.value.request_count == 0
        assert requests == [] and observation == original

    asyncio.run(run())


def test_scripted_observations_select_distinct_role_tools():
    async def run():
        policy = ScriptedPolicy()
        base = {
            "minerals": 160,
            "supply_cap": 20,
            "supply_used": 10,
            "workers": 12,
            "army_count": 4,
            "structures": {},
            "map": {"enemy_start": [90, 80]},
        }
        outputs = await asyncio.gather(*(policy.decide(role, base, []) for role in ROLES))
        assert [out["actions"][0]["tool"] for out in outputs] == [
            "train_worker",
            "build_barracks",
            "attack",
        ]
        assert all(out["model"] == "scripted-baseline" for out in outputs)
        assert all(out["usage"] == {"input_tokens": 0, "output_tokens": 0} for out in outputs)
        assert all(out["request_count"] == 0 and out["usage_reported"] for out in outputs)
        assert [state.turns for state in policy.states.values()] == [1, 1, 1]
        supply = await policy.decide("economy", {**base, "supply_used": 19}, [])
        assert supply["actions"][0]["tool"] == "build_supply"
        gather = await policy.decide("economy", {**base, "minerals": 0}, [])
        assert gather["actions"][0]["tool"] == "gather"
        train = await policy.decide("production", {**base, "structures": {"BARRACKS": 1}}, [])
        assert train["actions"][0]["tool"] == "train_marine"
        scout = await policy.decide("tactics", {**base, "army_count": 0}, [])
        assert scout["actions"][0]["tool"] == "scout"
        upgrade = await policy.decide(
            "production",
            {
                **base,
                "army_count": 8,
                "structures": {"BARRACKS": 1},
            },
            [],
        )
        assert upgrade["actions"][0]["tool"] == "build_engineering_bay"
        research = await policy.decide(
            "production",
            {
                **base,
                "vespene": 100,
                "structures": {"BARRACKS": 1, "ENGINEERINGBAY": 1},
            },
            [],
        )
        assert research["actions"][0]["tool"] == "research_infantry"

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        decision("attack"),
        decision(args={"arbitrary_code": "exec()"}),
        decision("build_supply", {"x": 1}),
        decision("build_supply", {"x": True, "y": 2}),
        decision("build_supply", {"x": float("nan"), "y": 2}),
        decision("gather", {"resource": "wood"}),
        decision("gather", {"max_workers": 0}),
        decision("gather", {"max_workers": 25}),
        decision("gather", {"max_workers": True}),
        decision("gather", {"max_workers": 1.5}),
        {**decision(), "actions": [decision()["actions"][0]] * 3},
        {**decision(), "messages": [{"to": "outsider", "text": "hello"}]},
    ],
)
def test_decision_rejects_role_and_argument_violations(payload):
    with pytest.raises(ValueError):
        validate_decision("economy", payload)


def test_llm_json_fences_thinking_redaction_and_private_fields():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            assert request.headers["Authorization"] == "Bearer secret-key-for-test"
            payload = decision()
            payload["reason"] = "<think>private chain</think>Public reason secret-key-for-test"
            text = "<think>private provider analysis</think>\n```json\n" + json.dumps(payload) + "\n```"
            return response(text, reasoning_content="must never reach timeline")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost:8000/v1", "Qwen3-8B", "secret-key-for-test", client=client)
            out = await policy.decide(
                "economy",
                {
                    "minerals": 100,
                    "api_key": "must not be sent",
                    "map": {"width": 100, "secret": "hidden"},
                    "units": [{"tag": 1, "owner": 1}, {"tag": 2, "owner": 2, "visible": False}],
                },
                [],
            )
            assert out["reason"] == "Public reason [redacted]"
            assert "private" not in out["raw_response"]
            assert "reasoning_content" not in out["raw_response"]
            assert out["usage"] == {"input_tokens": 12, "output_tokens": 8}
            assert out["usage_reported"] is True
            assert out["request_count"] == 1
            assert out["prompt_messages"] == requests[-1]["messages"]
            assert "secret-key-for-test" not in str(out["prompt_messages"])
            assert "Authorization" not in str(out["prompt_messages"])
            assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
            assert "must not be sent" not in str(requests)
            assert "hidden" not in str(requests)
            assert '"tag":2' not in str(requests)
            assert policy.states["economy"].turns == 1
            assert policy.states["tactics"].history == []

    asyncio.run(run())


def test_separate_contexts_and_bounded_history_with_shared_service():
    async def run():
        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            actor = next(role for role in ROLES if f"the {role} agent" in body["messages"][0]["content"])
            tool = {"economy": "train_worker", "production": "train_marine", "tactics": "hold"}[actor]
            return response(decision(tool))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy(
                "http://company-model/v1", "shared", client=client, history_turns=1, enable_thinking=None
            )
            await policy.decide("economy", {"minerals": 111}, [])
            await policy.decide("economy", {"minerals": 222}, [])
            await policy.decide("production", {"minerals": 333}, [])
            assert len(requests[1]["messages"]) == 4
            assert len(requests[2]["messages"]) == 2
            assert "111" not in str(requests[2])
            assert "chat_template_kwargs" not in requests[0]
            assert len(policy.states["economy"].history) == 2

    asyncio.run(run())


def test_invalid_json_has_bounded_retry_then_no_scripted_fallback():
    async def run():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return response("this is not JSON")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, max_attempts=2)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert caught.value.code == "invalid_output"
            assert caught.value.attempts == calls == 2
            assert caught.value.request_count == 2
            assert caught.value.usage == {"input_tokens": 24, "output_tokens": 16}
            assert caught.value.usage_reported is True
            assert policy.states["economy"].history == []

    asyncio.run(run())


def test_role_invalid_first_response_can_be_corrected_with_accounted_usage():
    async def run():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return response(decision("attack" if calls == 1 else "train_worker"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("economy", {}, [])
            assert calls == 2
            assert out["actions"][0]["tool"] == "train_worker"
            assert out["usage"] == {"input_tokens": 24, "output_tokens": 16}
            assert out["request_count"] == 2
            assert out["usage_reported"] is True
            assert "previous output was invalid" in out["prompt_messages"][-1]["content"]

    asyncio.run(run())


def test_timeout_has_one_total_deadline_and_no_history_commit():
    async def run():
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.1)
            return response(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, timeout=0.01, max_attempts=3)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert caught.value.code == "timeout"
            assert calls == 1
            assert caught.value.request_count == 1
            assert caught.value.usage == {"input_tokens": None, "output_tokens": None}
            assert caught.value.usage_reported is False
            assert policy.states["economy"].turns == 0

    asyncio.run(run())


def test_http_errors_do_not_reveal_service_body_credentials_or_retry_bad_requests():
    async def run():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(401, text="sensitive service content: api-key-value")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", "api-key-value", client=client)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert calls == 1
            assert caught.value.request_count == 1
            assert caught.value.usage_reported is False
            assert "api-key-value" not in str(caught.value)
            assert "sensitive" not in str(caught.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "base_url", ["", "ftp://server", "http://key@server/v1", "http://server/v1?key=secret"]
)
def test_explicit_service_endpoint_without_embedded_secrets_required(base_url):
    with pytest.raises(ValueError):
        LLMPolicy(base_url, "test")


def test_gather_resource_limits_and_scripted_periodic_ready_refinery_assignment():
    async def run():
        policy = ScriptedPolicy()
        base = {
            "minerals": 0,
            "workers": 16,
            "supply_used": 16,
            "supply_cap": 23,
            "structures": {"REFINERY": 1},
            "player_id": 1,
            "units": [{"tag": 99, "type": "REFINERY", "is_ready": True, "owner": "self", "owner_id": 1}],
        }
        for turn in range(1, 6):
            out = await policy.decide("economy", base, [])
            gather = next(action for action in out["actions"] if action["tool"] == "gather")
            assert gather["args"]["resource"] == ("gas" if turn in {1, 5} else "minerals")
            assert gather["args"]["max_workers"] == 3
        pending = ScriptedPolicy()
        out = await pending.decide(
            "economy",
            {
                **base,
                "units": [{"tag": 99, "type": "REFINERY", "is_ready": False, "owner": "self"}],
            },
            [],
        )
        assert out["actions"][0]["args"]["resource"] == "minerals"
        assert validate_decision("economy", decision("gather", {"resource": "gas", "max_workers": 24}))

    asyncio.run(run())


def test_missing_provider_usage_stays_unknown_instead_of_zero():
    async def run():
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision())}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("economy", {}, [])
            assert out["request_count"] == 1
            assert out["usage_reported"] is False
            assert out["usage"] == {"input_tokens": None, "output_tokens": None}

    asyncio.run(run())


def test_partially_reported_retry_usage_remains_explicitly_incomplete():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "invalid JSON"}}],
                        "usage": {"prompt_tokens": 10},
                    },
                )
            return response(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("economy", {}, [])
            assert out["request_count"] == 2
            assert out["usage_reported"] is False
            assert out["usage"] == {"input_tokens": 22, "output_tokens": None}
            assert out["prompt_messages"] == requests[-1]["messages"]

    asyncio.run(run())


def test_prompt_audit_contains_only_the_sanitized_request_messages():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", "secret-test-key", client=client)
            out = await policy.decide(
                "economy",
                {},
                [
                    {
                        "to": "economy",
                        "from": "production",
                        "text": "message secret-test-key",
                    }
                ],
            )
            assert out["prompt_messages"] == requests[-1]["messages"]
            assert "secret-test-key" not in str(out["prompt_messages"])
            assert "[redacted]" in str(out["prompt_messages"])
            assert "Authorization" not in str(out["prompt_messages"])

    asyncio.run(run())


@pytest.mark.parametrize(
    "actor,building,forbidden",
    [
        ("economy", "SUPPLYDEPOT", "build_supply"),
        ("economy", "REFINERY", "build_refinery"),
        ("production", "BARRACKS", "build_barracks"),
        ("production", "ENGINEERINGBAY", "build_engineering_bay"),
    ],
)
def test_pending_owned_buildings_do_not_trigger_duplicate_construction(actor, building, forbidden):
    async def run():
        base = {
            "minerals": 175,
            "workers": 16,
            "supply_cap": 23,
            "supply_used": 16,
            "army_count": 8,
            "structures": {"BARRACKS": 1} if building == "ENGINEERINGBAY" else {},
            "units": [{"tag": 77, "type": building, "owner": "self", "is_ready": False}],
        }
        if building == "SUPPLYDEPOT":
            base["supply_used"] = 22
        out = await ScriptedPolicy().decide(actor, base, [])
        assert forbidden not in [action["tool"] for action in out["actions"]]
        if building == "BARRACKS":
            assert "train_marine" not in [action["tool"] for action in out["actions"]]

    asyncio.run(run())


def test_model_receives_explicit_task_and_recent_execution_receipts():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision())

        task = {
            "scenario": "build_and_scout",
            "objective": "Build a small base and scout the opposing region",
            "role": "economy",
            "role_objective": "Reach 14 workers and a ready supply depot",
            "milestones": [{"name": "workers", "observed": 12, "target": 14, "done": False}],
        }
        receipts = [
            {
                "command_id": "cmd-1",
                "actor_id": "economy",
                "tool": "build_supply",
                "status": "rejected",
                "reason": "No idle worker",
            }
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("economy", {"task": task, "last_receipts": receipts}, [])
            public_observation = json.loads(requests[-1]["messages"][-1]["content"])["observation"]
            assert public_observation["task"] == task
            assert public_observation["last_receipts"] == receipts
            assert "task.objective" in requests[-1]["messages"][0]["content"]

    asyncio.run(run())


def test_canceled_inflight_request_remains_in_readonly_accounting():
    async def run():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.sleep(10)
            return response(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            task = asyncio.create_task(policy.decide("economy", {}, []))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            row = policy.accounting["economy"]
            assert row["calls"] == row["request_count"] == 1
            assert row["usage"] == {"input_tokens": None, "output_tokens": None}
            assert row["input_tokens"] is None and row["output_tokens"] is None
            assert row["usage_reported"] is False
            assert policy.accounting["production"]["calls"] == 0
            row["calls"] = 500
            assert policy.accounting["economy"]["calls"] == 1
            assert policy.states["economy"].turns == 0

    asyncio.run(run())


def test_lifetime_accounting_counts_failed_corrections_and_subsequent_decisions():
    async def run():
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return response("bad" if count == 1 else decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("economy", {}, [])
            await policy.decide("economy", {}, [])
            row = policy.accounting["economy"]
            assert row["calls"] == row["request_count"] == 3
            assert row["usage_reported"] is True
            assert row["usage"] == {"input_tokens": 36, "output_tokens": 24}
            assert row["input_tokens"] == 36 and row["output_tokens"] == 24

    asyncio.run(run())


def test_role_json_schema_and_tool_prerequisites_with_explicit_compatibility_switch():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("train_marine"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            strict = LLMPolicy("http://localhost/v1", "test", client=client)
            await strict.decide("production", {}, [])
            strict_format = requests[-1]["response_format"]
            assert strict_format["type"] == "json_schema"
            actions = strict_format["json_schema"]["schema"]["properties"]["actions"]["items"]["oneOf"]
            tools = {variant["properties"]["tool"]["const"] for variant in actions}
            assert tools == {"build_barracks", "build_engineering_bay", "train_marine", "research_infantry"}
            prompt = requests[-1]["messages"][0]["content"]
            assert "100 vespene" in prompt and "ready idle engineering bay" in prompt
            assert "is_ready=false" in prompt
            compatible = LLMPolicy("http://localhost/v1", "test", client=client, strict_schema=False)
            await compatible.decide("production", {}, [])
            assert requests[-1]["response_format"] == {"type": "json_object"}

    asyncio.run(run())


def test_history_is_historical_summary_and_current_input_has_bounded_role_units():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision())

        units = [
            {
                "tag": index + 1,
                "type": "SCV",
                "owner": "self",
                "x": index,
                "y": 1,
                "is_ready": True,
                "is_structure": False,
            }
            for index in range(100)
        ] + [
            {
                "tag": 1001,
                "type": "REFINERY",
                "owner": "self",
                "is_ready": False,
                "is_structure": True,
                "is_idle": False,
            }
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("economy", {"workers": 100, "units": units, "game_time": 5}, [])
            await policy.decide("economy", {"workers": 101, "units": units, "game_time": 10}, [])
            previous = json.loads(requests[-1]["messages"][1]["content"])
            assert previous["kind"] == "historical-summary-not-current-state"
            assert previous["workers"] == 100 and previous["pending_counts"] == {"REFINERY": 1}
            assert "units" not in previous and "map" not in previous
            current = json.loads(requests[-1]["messages"][-1]["content"])["observation"]
            assert current["workers"] == 101
            assert len(current["units"]) <= 48
            assert current["input_projection"]["omitted_units"] > 0
            assert any(unit["type"] == "REFINERY" and not unit["is_ready"] for unit in current["units"])

    asyncio.run(run())


def test_dynamic_schema_uses_current_native_role_tools_and_empty_actions_are_legal():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        observation = {
            "action_state": {
                "train_worker": {"available": True, "unit_tags": [1]},
                "build_supply": {"available": False, "reason": "A depot is already under construction"},
                "attack": {"available": True, "unit_tags": [2]},
            },
            "task": {"role_done": True, "role_phase": "complete", "goal_gaps": []},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("economy", observation, [])
            schema = requests[0]["response_format"]["json_schema"]["schema"]
            assert [
                item["properties"]["tool"]["const"]
                for item in schema["properties"]["actions"]["items"]["oneOf"]
            ] == ["train_worker"]
            assert out["actions"] == []
            assert out["request_count"] == 1 and out["guidance_enabled"] is True
            assert out["available_tools"] == ["train_worker"]
            prompt = requests[0]["messages"][0]["content"]
            assert "actions=[]" in prompt and "role_done" in prompt
            assert "Your allowed tools for THIS decision ONLY: train_worker." in prompt
            assert "build_supply:" in prompt and "expand:" in prompt  # Knowledge survives masking.
            current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
            assert current["task"]["role_done"] is True
            assert current["action_context"]["allowed_tools"] == ["train_worker"]

    asyncio.run(run())


def test_all_blocked_schema_requires_empty_actions_without_invalid_empty_union():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide(
                "tactics", {"action_state": {"attack": {"available": False, "reason": "No army"}}}, []
            )
            action_schema = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["actions"]
            assert action_schema["maxItems"] == 0
            assert action_schema["items"] == {"type": "object"}
            assert out["actions"] == [] and out["request_count"] == 1
            assert "No army" in requests[0]["messages"][-1]["content"]

    asyncio.run(run())


def test_unavailable_tool_is_rejected_even_with_compatible_non_strict_provider_and_corrected():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("build_supply") if len(requests) == 1 else {**decision(), "actions": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, strict_schema=False)
            out = await policy.decide(
                "economy",
                {"action_state": {"build_supply": {"available": False, "reason": "Builder busy"}}},
                [],
            )
            assert out["request_count"] == 2 and out["actions"] == []
            assert "not currently available" in requests[1]["messages"][-1]["content"]
            assert out["usage"] == {"input_tokens": 24, "output_tokens": 16}

    asyncio.run(run())


def test_guidance_disabled_preserves_static_schema_old_task_and_legacy_prompt():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("build_supply"))

        observation = {
            "guidance_enabled": False,
            "action_state": {"build_supply": {"available": False, "reason": "Builder busy"}},
            "action_context": {"allowed_tools": []},
            "task": {
                "objective": "Develop",
                "role_objective": "Workers and supply",
                "role_done": True,
                "role_phase": "complete",
            },
            "queued_counts": {"MARINE": 2},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("economy", observation, [])
            assert out["guidance_enabled"] is False
            assert out["actions"] == [{"tool": "build_supply", "args": {}}]
            schema = requests[0]["response_format"]["json_schema"]["schema"]
            assert len(schema["properties"]["actions"]["items"]["oneOf"]) == 5
            current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
            assert current["task"] == {"objective": "Develop", "role_objective": "Workers and supply"}
            assert "action_context" not in current and "queued_counts" not in current
            prompt = requests[0]["messages"][0]["content"]
            assert "actual milestone targets for the selected scenario" in prompt
            assert "14 workers" not in prompt and "four ready marines" not in prompt

    asyncio.run(run())


def test_new_native_progress_and_task_stage_survive_model_projection_with_latest_feedback():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        order = {
            "ability_id": 560,
            "ability": "TRAIN_MARINE",
            "progress": 0.4,
            "target": None,
            "produces": "MARINE",
            "researches": None,
        }
        task = {
            "objective": "Complete the experiment",
            "current_stage": {"id": "army", "policy_goal": "Four ready marines"},
            "role_phase": "active",
            "role_done": False,
            "role_milestones": [{"id": "marines", "value": 1, "target": 4, "known": True, "achieved": False}],
            "goal_gaps": [{"metric": "ready_counts.MARINE", "remaining": 3}],
            "focus": "army",
            "remaining_milestones": ["marines"],
            "stage_goals": ["Four ready marines"],
        }
        observation = {
            "task": task,
            "ready_counts": {"MARINE": 1, "BARRACKS": 1},
            "pending_counts": {"SUPPLYDEPOT": 1},
            "queued_counts": {"MARINE": 2},
            "production": [
                {
                    "tag": 4,
                    "type": "BARRACKS",
                    "is_ready": True,
                    "is_busy": True,
                    "queue_length": 1,
                    "orders": [order],
                }
            ],
            "worker_state": {"eligible_builder_count": 2, "constructing_tags": [9]},
            "units": [
                {
                    "tag": 4,
                    "type": "BARRACKS",
                    "owner": "self",
                    "is_structure": True,
                    "is_ready": True,
                    "build_progress": 1.0,
                    "orders": [order],
                }
            ],
            "last_receipts": [
                {
                    "actor_id": "production",
                    "tool": "train_marine",
                    "status": "rejected",
                    "reason": "No ready idle production building can currently train MARINE",
                }
            ],
            "action_state": {"train_marine": {"available": False, "reason": "Barracks is busy"}},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("production", observation, [])
            current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
            assert current["task"] == task
            assert current["ready_counts"]["MARINE"] == 1 and current["queued_counts"]["MARINE"] == 2
            assert current["units"][0]["orders"] == [order]
            assert current["production"][0]["queue_length"] == 1
            assert current["worker_state"]["eligible_builder_count"] == 2
            assert current["action_context"]["recent_rejections"][0]["tool"] == "train_marine"
            assert "No ready idle production" in current["action_context"]["recent_rejections"][0]["reason"]

    asyncio.run(run())


@pytest.mark.parametrize("resources", [["gas"], ["minerals"], ["minerals", "gas"]])
def test_dynamic_gather_schema_and_validator_agree_on_available_resource_defaults(resources):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("gather", {"resource": resources[0], "max_workers": 3}))

        observation = {
            "action_state": {"gather": {"available": True, "resources": resources, "unit_tags": [1]}}
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("economy", observation, [])
            variant = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["actions"][
                "items"
            ]["oneOf"][0]
            args_schema = variant["properties"]["args"]
            assert args_schema["properties"]["resource"]["enum"] == resources
            assert ("resource" in args_schema.get("required", [])) is ("minerals" not in resources)
            assert out["actions"][0]["args"]["resource"] == resources[0]
            assert out["request_count"] == 1

    asyncio.run(run())


def test_gas_only_rejects_implicit_minerals_then_accepts_explicit_gas_and_old_calls_still_work():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("gather", {} if len(requests) == 1 else {"resource": "gas"}))

        observation = {"action_state": {"gather": {"available": True, "resources": ["gas"]}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, strict_schema=False)
            out = await policy.decide("economy", observation, [])
            assert out["request_count"] == 2 and out["actions"][0]["args"] == {"resource": "gas"}
            assert "omitting it means minerals" in requests[1]["messages"][-1]["content"]
            assert (
                "only available gather resources this turn are: gas" in requests[0]["messages"][0]["content"]
            )
        assert validate_decision("economy", decision("gather"))["actions"][0]["args"] == {}
        with pytest.raises(ValueError, match="not currently available"):
            validate_decision("economy", decision("gather"), gather_resources=["gas"])
        assert validate_decision("economy", decision("gather"), gather_resources=["minerals"])

    asyncio.run(run())


def test_native_harvester_capacity_is_preserved_without_raw_worker_assignment_lists():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        observation = {
            "worker_state": {
                "gas_worker_count": 1,
                "mineral_worker_count": 12,
                "unknown_harvest_worker_count": 2,
                "gas_assignments": [
                    {
                        "target_tag": 99,
                        "worker_count": 1,
                        "worker_tags": [3],
                        "assigned_harvesters": 3,
                        "ideal_harvesters": 3,
                        "remaining_capacity": 0,
                    }
                ],
                "mineral_assignments": [
                    {"target_tag": 100, "worker_count": 12, "worker_tags": list(range(1, 13))}
                ],
            },
            "units": [
                {
                    "tag": 99,
                    "type": "REFINERY",
                    "owner": "self",
                    "is_structure": True,
                    "assigned_harvesters": None,
                    "ideal_harvesters": 3,
                }
            ],
            "map": {
                "width": 64,
                "height": 64,
                "expansions": [[10, 10]],
                "resource_diagnostics": [{"private_debug": "not model input"}] * 200,
            },
            "task": {
                "role_done": True,
                "role_milestones": [{"metric": "supply_cap", "target": 30, "maintain": True}],
            },
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("economy", observation, [])
            current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
            assert current["worker_state"]["gas_assignments"][0] == {
                "target_tag": 99,
                "worker_count": 1,
                "assigned_harvesters": 3,
                "ideal_harvesters": 3,
                "remaining_capacity": 0,
            }
            assert "worker_tags" not in str(current["worker_state"])
            assert current["worker_state"]["unknown_harvest_worker_count"] == 2
            assert current["units"][0]["assigned_harvesters"] is None
            assert "resource_diagnostics" not in current["map"]
            assert current["task"]["role_milestones"][0]["maintain"] is True
            prompt = requests[0]["messages"][0]["content"]
            assert "release shared resources" in prompt and "returning cargo" in prompt

    asyncio.run(run())


def staged_observation(role, goals, *, phase="expansion", allowed=(), **extra):
    return {
        "minerals": 500,
        "vespene": 200,
        "supply_used": 20,
        "supply_cap": 31,
        "workers": 20,
        "ready_counts": {"COMMANDCENTER": 1},
        "pending_counts": {},
        "queued_counts": {},
        "action_state": {tool: {"available": True} for tool in allowed},
        "task": {
            "scenario": "expansion_logistics",
            "role": role,
            "role_phase": phase,
            "current_stage": {"id": phase},
            "role_done": False,
            "role_milestones": goals,
        },
        **extra,
    }


def test_scripted_stage_can_reach_24_workers_expand_and_wait_for_pending_base():
    async def run():
        policy = ScriptedPolicy()
        goals = [
            {"metric": "workers", "target": 24, "done": False},
            {"metric": "expanded_bases", "target": 2, "observed": 1, "done": False},
        ]
        observation = staged_observation("economy", goals, allowed=["train_worker", "expand"])
        out = await policy.decide("economy", observation, [])
        assert out["baseline_kind"] == "mission-stage-scripted" and out["model"] == "scripted-baseline"
        assert [item["tool"] for item in out["actions"]] == ["train_worker", "expand"]
        pending = await policy.decide(
            "economy", {**observation, "workers": 24, "pending_counts": {"COMMANDCENTER": 1}}, []
        )
        assert pending["actions"] == []
        assert out["request_count"] == 0 and out["usage"] == {"input_tokens": 0, "output_tokens": 0}

    asyncio.run(run())


def test_scripted_stage_gas_requests_are_small_legal_and_stop_when_no_capacity():
    async def run():
        policy = ScriptedPolicy()
        goals = [
            {"metric": "structures", "types": ["REFINERY"], "target": 2, "done": False},
            {"metric": "collected_vespene", "target": 200, "done": False},
        ]
        observation = staged_observation(
            "economy",
            goals,
            phase="gas_logistics",
            allowed=["build_refinery", "gather"],
            ready_counts={"REFINERY": 1},
        )
        observation["action_state"]["gather"]["resources"] = ["gas"]
        out = await policy.decide("economy", observation, [])
        assert out["actions"] == [
            {"tool": "build_refinery", "args": {}},
            {"tool": "gather", "args": {"resource": "gas", "max_workers": 3}},
        ]
        observation["ready_counts"] = {"REFINERY": 2}
        observation["action_state"]["gather"]["resources"] = []
        assert (await policy.decide("economy", observation, []))["actions"] == []

    asyncio.run(run())


def test_scripted_readiness_respects_queues_upgrade_prerequisite_and_maintained_supply():
    async def run():
        policy = ScriptedPolicy()
        upgrade = "TERRANINFANTRYWEAPONSLEVEL1"
        goals = [
            {"metric": "upgrade", "upgrade": upgrade, "target": 1, "done": False},
            {
                "metric": "units",
                "types": ["MARINE"],
                "target": 12,
                "requires_upgrade": upgrade,
                "done": False,
            },
        ]
        observation = staged_observation(
            "production",
            goals,
            phase="upgraded_army",
            allowed=["research_infantry", "train_marine"],
            ready_counts={"BARRACKS": 2, "ENGINEERINGBAY": 1, "MARINE": 10},
            queued_counts={"MARINE": 2},
        )
        first = await policy.decide("production", observation, [])
        assert first["actions"] == [{"tool": "research_infantry", "args": {}}]
        observation["upgrades"] = {"completed": [upgrade], "in_progress": []}
        assert (await policy.decide("production", observation, []))["actions"] == []
        observation["queued_counts"] = {}
        assert (await policy.decide("production", observation, []))["actions"] == [
            {"tool": "train_marine", "args": {}}
        ]
        supply = staged_observation(
            "economy",
            [{"metric": "supply_cap", "target": 30, "maintain": True, "done": False}],
            phase="upgraded_army",
            allowed=["build_supply"],
            supply_cap=23,
        )
        assert (await policy.decide("economy", supply, []))["actions"] == [
            {"tool": "build_supply", "args": {}}
        ]

    asyncio.run(run())


def test_scripted_stage_done_is_empty_but_native_victory_uses_separate_full_game_baseline():
    async def run():
        policy = ScriptedPolicy()
        observation = staged_observation("tactics", [], allowed=["scout", "attack"], army_count=8)
        observation["task"]["role_done"] = True
        assert (await policy.decide("tactics", observation, []))["actions"] == []
        observation["task"].update(scenario="full_match", role_done=False, current_stage={"id": "victory"})
        out = await policy.decide("tactics", observation, [])
        assert out["baseline_kind"] == "legacy-full-game-scripted"
        assert out["actions"] == [{"tool": "attack", "args": {}}]

    asyncio.run(run())


@pytest.mark.parametrize("default_available", [True, False])
def test_scout_schema_matches_native_default_identity_and_explicit_candidate_permissions(default_available):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("scout", {} if default_available else {"unit_tag": 11}))

        observation = {
            "action_state": {
                "scout": {
                    "available": True,
                    "unit_tags": [7, 11],
                    "default_unit_tag": 7,
                    "default_available": default_available,
                }
            }
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("tactics", observation, [])
            variants = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["actions"][
                "items"
            ]["oneOf"][0]["properties"]["args"]["oneOf"]
            assert any(variant["required"] == [] for variant in variants) is default_available
            assert all(
                variant["properties"]["unit_tag"]["enum"] == [7, 11]
                for variant in variants
                if "unit_tag" in variant["properties"]
            )
            if not default_available:
                assert all("unit_tag" in variant["required"] for variant in variants)
            assert out["request_count"] == 1
            assert out["actions"][0]["args"] == ({} if default_available else {"unit_tag": 11})

    asyncio.run(run())


def test_scout_default_rejection_is_corrected_without_rewriting_model_output_or_inventing_tags():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("scout", {} if len(requests) == 1 else {"unit_tag": 11}))

        state = {"available": True, "unit_tags": [11], "default_unit_tag": 7, "default_available": False}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, strict_schema=False)
            out = await policy.decide("tactics", {"action_state": {"scout": state}}, [])
            assert out["request_count"] == 2 and out["actions"][0]["args"] == {"unit_tag": 11}
            assert "Default scout is unavailable" in requests[1]["messages"][-1]["content"]
            assert out["usage"] == {"input_tokens": 24, "output_tokens": 16}
        for args in ({}, {"unit_tag": 7}, {"unit_tag": 999}):
            with pytest.raises(ValueError):
                validate_decision("tactics", decision("scout", args), scout_state=state)
        assert validate_decision("tactics", decision("scout", {"unit_tag": 11}), scout_state=state)

    asyncio.run(run())


@pytest.mark.parametrize(
    "guided,first_scenario,first_phase,next_scenario,next_phase,should_reset",
    [
        (True, "expansion_and_victory", "expansion", "expansion_and_victory", "victory", True),
        (True, "expansion_logistics", "opening", "army_readiness", "opening", True),
        (True, "expansion_and_victory", "victory", "expansion_and_victory", "victory", False),
        (False, "expansion_and_victory", "expansion", "expansion_and_victory", "victory", False),
        (False, "expansion_logistics", "opening", "army_readiness", "opening", False),
    ],
)
def test_phase_history_reset_only_changes_guided_cross_phase_requests(
    guided, first_scenario, first_phase, next_scenario, next_phase, should_reset
):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision("train_marine"), "goal": "A model-selected public goal"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, history_turns=3)
            first = await policy.decide(
                "production",
                {
                    "guidance_enabled": guided,
                    "game_time": 100,
                    "task": {"scenario": first_scenario, "role_phase": first_phase},
                },
                [],
            )
            preserved_trace = json.dumps(first, sort_keys=True)
            second = await policy.decide(
                "production",
                {
                    "guidance_enabled": guided,
                    "game_time": 390,
                    "task": {"scenario": next_scenario, "role_phase": next_phase},
                },
                [],
            )
            assert len(requests[1]["messages"]) == (2 if should_reset else 4)
            assert second["context_reset"]["reset"] is should_reset
            assert second["context_reset"]["discarded_messages"] == (2 if should_reset else 0)
            if should_reset:
                assert second["context_reset"]["from"] == {
                    "scenario": first_scenario,
                    "role_phase": first_phase,
                }
                assert second["context_reset"]["to"] == {"scenario": next_scenario, "role_phase": next_phase}
                assert "historical-summary-not-current-state" not in str(requests[1]["messages"])
            else:
                assert json.loads(requests[1]["messages"][1]["content"])["game_time"] == 100
            assert json.dumps(first, sort_keys=True) == preserved_trace
            assert second["actions"] == [{"tool": "train_marine", "args": {}}]
            assert second["request_count"] == 1 and policy.accounting["production"]["calls"] == 2

    asyncio.run(run())


def test_phase_handoff_and_safe_message_freshness_are_visible_without_fabricating_completion():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        handoff = {
            "from_phase": "expansion",
            "to_phase": "victory",
            "observed_game_time": 390,
            "completed_stages": ["opening", "expansion"],
            "mission_complete": False,
        }
        task = {
            "scenario": "expansion_and_victory",
            "role_phase": "victory",
            "role_done": False,
            "continuous_role_duty": True,
            "phase_handoff": handoff,
        }
        messages = [
            {
                "from": "economy",
                "to": "production",
                "text": "Current budget is shared",
                "phase": "victory",
                "round": 40,
                "game_time": 390.0,
            },
            {
                "from": "tactics",
                "to": "production",
                "text": "Untrusted malformed metadata",
                "phase": {"bad": True},
                "round": True,
                "game_time": float("nan"),
            },
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("production", {"task": task}, messages)
            current = json.loads(requests[0]["messages"][-1]["content"])
            assert current["observation"]["task"]["phase_handoff"] == handoff
            assert current["observation"]["task"]["continuous_role_duty"] is True
            assert current["messages"][0] == {
                key: messages[0][key] for key in ("from", "text", "phase", "round", "game_time")
            }
            assert current["messages"][1] == {"from": "tactics", "text": "Untrusted malformed metadata"}
            prompt = requests[0]["messages"][0]["content"]
            assert "ongoing native-Victory task" in prompt
            assert "until observed native Victory" in prompt
            await policy.decide("production", {"task": task, "guidance_enabled": False}, messages)
            legacy = json.loads(requests[1]["messages"][-1]["content"])
            assert "phase_handoff" not in legacy["observation"]["task"]
            assert legacy["messages"][0] == {"from": "economy", "text": "Current budget is shared"}

    asyncio.run(run())


def saved_policy_history(pairs=2, content_length=0):
    rows = []
    for index in range(pairs):
        rows.extend(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "kind": "historical-summary-not-current-state",
                            "game_time": index,
                            "padding": "x" * content_length,
                        }
                    ),
                },
                {"role": "assistant", "content": json.dumps(decision())},
            ]
        )
    return rows


def test_context_overflow_prunes_oldest_pair_and_retries_without_faking_unreported_usage():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "exceed_context_size_error",
                            "code": 400,
                            "message": "private-provider-content",
                        }
                    },
                )
            return response(decision())

        original = saved_policy_history()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, max_attempts=2)
            policy.states["economy"].history = json.loads(json.dumps(original))
            out = await policy.decide("economy", {"minerals": 123}, [])
            assert len(requests) == out["request_count"] == 2
            assert len(requests[0]["messages"]) == 6 and len(requests[1]["messages"]) == 4
            assert requests[1]["messages"][1:3] == original[2:]
            assert requests[1]["messages"][0] == requests[0]["messages"][0]
            assert requests[1]["messages"][-1] == requests[0]["messages"][-1]
            assert out["prompt_messages"] == requests[1]["messages"]
            assert out["usage"] == {"input_tokens": None, "output_tokens": None}
            assert out["usage_reported"] is False
            assert policy.accounting["economy"]["calls"] == 2
            assert policy.accounting["economy"]["usage_reported"] is False
            assert out["context_recovery"] == {
                "overflow_errors": 1,
                "headroom_errors": 0,
                "recovery_attempts": 1,
                "trimmed_history_pairs": 1,
                "proactive_trimmed_pairs": 0,
            }
            assert "private-provider-content" not in str(out)

    asyncio.run(run())


def test_repeated_context_overflow_stops_at_attempt_budget_and_keeps_usage_unknown():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "type": "invalid_request_error",
                        "message": "private-provider-content",
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, max_attempts=3)
            policy.states["economy"].history = saved_policy_history(3)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert len(requests) == caught.value.request_count == 3
            assert [len(body["messages"]) for body in requests] == [8, 6, 4]
            assert caught.value.code == "context_overflow"
            assert caught.value.context_recovery["recovery_attempts"] == 2
            assert caught.value.context_recovery["trimmed_history_pairs"] == 2
            assert caught.value.usage == {"input_tokens": None, "output_tokens": None}
            assert caught.value.usage_reported is False
            assert "private-provider-content" not in str(caught.value) + str(caught.value.prompt_messages)

    asyncio.run(run())


@pytest.mark.parametrize(
    "error,with_history,code",
    [
        ({"error": {"type": "exceed_context_size_error"}}, False, "context_overflow"),
        (
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "context mentioned but unrelated private error",
                }
            },
            True,
            "service_error",
        ),
        ({"error": "context_length_exceeded"}, True, "service_error"),
    ],
)
def test_noncontext_400_and_unrecoverable_context_do_not_retry(error, with_history, code):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(400, json=error)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, max_attempts=3)
            if with_history:
                policy.states["economy"].history = saved_policy_history()
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert len(requests) == caught.value.request_count == 1
            assert caught.value.code == code
            assert caught.value.context_recovery["recovery_attempts"] == 0
            assert caught.value.usage_reported is False

    asyncio.run(run())


def test_proactive_history_budget_includes_system_and_retains_current_observation():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            policy._system_prompt = lambda *args, **kwargs: "S" * 8000
            policy.states["economy"].history = saved_policy_history(3, 6000)
            out = await policy.decide("economy", {"minerals": 9876}, [])
            assert out["request_count"] == 1 and out["usage_reported"] is True
            assert out["context_recovery"]["proactive_trimmed_pairs"] > 0
            assert sum(len(message["content"]) for message in requests[0]["messages"]) <= 24000
            assert json.loads(requests[0]["messages"][-1]["content"])["observation"]["minerals"] == 9876

    asyncio.run(run())


def test_combat_projection_shows_current_damage_and_cumulative_history_without_hidden_enemy_memory():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("attack"))

        task = {"objective": "Win the native game", "role_phase": "victory", "continuous_role_duty": True}
        first = {
            "task": task,
            "army_count": 1,
            "ready_counts": {"MARINE": 1},
            "queued_counts": {"MARINE": 1},
            "score": {"source": "sc2_native_score", "lost_minerals": {"army": 50}},
            "action_state": {"attack": {"available": True, "unit_tags": [1]}},
            "units": [
                {
                    "tag": 1,
                    "owner": "self",
                    "type": "MARINE",
                    "is_structure": False,
                    "is_ready": True,
                    "health": 45,
                    "health_max": 45,
                },
                {"tag": 20, "owner": "enemy", "type": "ZERGLING", "visible": True},
            ],
        }
        second = {
            **first,
            "score": {"source": "sc2_native_score", "lost_minerals": {"army": 150}},
            "units": [
                {
                    "tag": 1,
                    "owner": "self",
                    "type": "MARINE",
                    "is_structure": False,
                    "is_ready": True,
                    "health": 9,
                    "health_max": 45,
                },
                {"tag": 20, "owner": "enemy", "type": "ZERGLING", "visible": False},
            ],
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("tactics", first, [])
            out = await policy.decide("tactics", second, [])
            current = json.loads(requests[1]["messages"][-1]["content"])["observation"]
            assert current["combat_status"]["visible_enemy_counts"] == {}
            assert current["combat_status"]["most_damaged_army"][0]["health"] == 9
            assert current["combat_status"]["army_score_totals"]["lost_minerals"] == 150
            past = json.loads(requests[1]["messages"][1]["content"])
            assert past["army_score_totals"]["lost_minerals"] == 50
            assert "ZERGLING" not in json.dumps(current)
            assert "loss_delta" not in json.dumps(current)
            # Guidance does not silently reject an attack or invent a minimum force size.
            assert out["actions"] == [{"tool": "attack", "args": {}}]
            assert out["request_count"] == 1 and out["model"] == "test"
            prompt = requests[1]["messages"][0]["content"]
            assert "rendezvous location" in prompt and "no prescribed force size" in prompt
            assert "actions=[] preserves existing orders" in prompt
            assert "does not maintain an attack" in prompt
            assert "not that the enemy base is empty, visible or vulnerable" in prompt
            assert "army_positioning" in prompt
            # The prompt clarifies semantics but still returns the model's attack;
            # no hidden retreat, hold or minimum-army policy is substituted.
            assert current["combat_status"]["army_orders"]["unknown_order_units"] == 1

    asyncio.run(run())


def test_production_semantics_and_execution_failure_summary_do_not_change_tool_permissions_or_goals():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(decision("build_engineering_bay"))

        task = {
            "objective": "Win",
            "role_milestones": [{"metric": "native_victory", "target": 1, "done": False}],
        }
        receipt = {
            "command_id": "cmd-1",
            "actor_id": "production",
            "tool": "build_engineering_bay",
            "status": "accepted",
            "details": {"execution_status": "failed", "native_errors": []},
        }
        observation = {
            "task": task,
            "ready_counts": {"ENGINEERINGBAY": 2, "BARRACKS": 1},
            "queued_counts": {"MARINE": 1},
            "last_receipts": [receipt],
            "action_state": {
                "build_engineering_bay": {"available": True},
                "train_marine": {"available": False, "reason": "busy"},
            },
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("production", observation, [])
            current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
            failure = current["action_context"]["recent_execution_failures"][0]
            assert failure["ack_status"] == "accepted" and failure["execution_status"] == "failed"
            assert current["last_receipts"][0]["status"] == "accepted"
            assert current["task"] == task and observation["task"] == task
            assert out["actions"] == [{"tool": "build_engineering_bay", "args": {}}]
            prompt = requests[0]["messages"][0]["content"]
            assert "cannot train Marines" in prompt and "waiting is valid" in prompt
            assert "recent_execution_failures" in prompt

    asyncio.run(run())


def test_blocked_tool_knowledge_and_cross_role_prerequisites_do_not_expand_schema_or_rewrite_output():
    async def run():
        requests = []
        # This reproduces the real semantic contradiction. The policy exposes it
        # for audit; it does not silently replace an Engineering Bay with Barracks.
        contradictory = {
            **decision("build_engineering_bay"),
            "goal": "Build Barracks for infantry production",
            "reason": "Proceed with Barracks construction",
        }

        def handler(request):
            requests.append(json.loads(request.content))
            return response(contradictory)

        observation = {
            "minerals": 220,
            "supply_cap": 15,
            "supply_used": 12,
            "ready_counts": {"COMMANDCENTER": 1, "SCV": 12},
            "pending_counts": {},
            "task": {"scenario": "full_match", "objective": "Win the native SC2 game"},
            "action_state": {
                "build_supply": {"available": True},
                "build_barracks": {"available": False, "reason": "no_eligible_unit_or_native_ability"},
                "build_engineering_bay": {"available": True},
                "train_marine": {"available": False, "reason": "no_eligible_unit_or_native_ability"},
            },
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            out = await policy.decide("production", observation, [])
            prompt = requests[0]["messages"][0]["content"]
            assert "build_barracks: Builds a Barracks: 150 minerals, a completed ready Supply Depot" in prompt
            assert "economy owns:" in prompt and "build_supply: Builds a Supply Depot" in prompt
            assert "currently blocked tools (knowledge, not permission)" in prompt
            assert "not a substitute for a blocked tool with different effects" in prompt
            assert "An Engineering Bay is research infrastructure and cannot train Marines" in prompt
            schema = requests[0]["response_format"]["json_schema"]["schema"]
            variants = schema["properties"]["actions"]["items"]["oneOf"]
            assert [variant["properties"]["tool"]["const"] for variant in variants] == [
                "build_engineering_bay"
            ]
            current = json.loads(requests[0]["messages"][-1]["content"])["observation"]
            assert current["action_context"]["team_blockers"]["production"]["build_barracks"] == (
                "no_eligible_unit_or_native_ability"
            )
            assert "SUPPLYDEPOT" not in current["ready_counts"]
            assert out["actions"] == contradictory["actions"]
            assert out["goal"] == contradictory["goal"]
            assert out["request_count"] == 1
            with pytest.raises(ValueError):
                validate_decision(
                    "production", decision("build_barracks"), allowed_tools=["build_engineering_bay"]
                )

    asyncio.run(run())


def test_continuous_victory_prompt_separates_ongoing_economy_from_staged_completion_without_choosing_actions():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        base = {
            "minerals": 220,
            "workers": 12,
            "supply_used": 12,
            "supply_cap": 15,
            "worker_state": {"idle_tags": [1, 2], "harvesting_tags": [3, 4]},
            "action_state": {
                "build_supply": {"available": True},
                "gather": {"available": True, "resources": ["minerals"]},
                "build_barracks": {"available": False, "reason": "no_eligible_unit_or_native_ability"},
            },
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            for task in (
                {"scenario": "full_match"},
                {"scenario": "expansion_and_victory", "continuous_role_duty": True},
                {"scenario": "build_and_scout", "role_done": True},
            ):
                out = await policy.decide("economy", {**base, "task": task}, [])
                prompt = requests[-1]["messages"][0]["content"]
                assert "A Depot supplies population and unlocks Barracks when completed" in prompt
                current = json.loads(requests[-1]["messages"][-1]["content"])["observation"]
                assert current["action_context"]["worker_saturation"]["idle"] == 2
                assert current["action_context"]["team_blockers"]["production"]["build_barracks"] == (
                    "no_eligible_unit_or_native_ability"
                )
                if task["scenario"] == "build_and_scout":
                    assert "prefer actions=[] and release shared resources" in prompt
                    assert "ongoing native-Victory task" not in prompt
                else:
                    assert "ongoing native-Victory task" in prompt
                    assert "prefer actions=[] and release shared resources" not in prompt
                assert out["actions"] == [] and out["request_count"] == 1
            await policy.decide("economy", {**base, "guidance_enabled": False}, [])
            assert "Full team tool catalog" not in requests[-1]["messages"][0]["content"]

    asyncio.run(run())


def test_thinking_request_has_bounded_parser_profile_and_never_retains_private_reasoning():
    async def run():
        requests = []
        secret_field = "PRIVATE_FIELD_REASONING_DO_NOT_PERSIST"
        secret_inline = "PRIVATE_INLINE_REASONING_DO_NOT_PERSIST"

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": f"<think>{secret_inline}</think>" + json.dumps(decision()),
                                "reasoning_content": secret_field,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    # Provider counts the complete generation, including thinking.
                    "usage": {"prompt_tokens": 100, "completion_tokens": 550},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy(
                "http://localhost/v1",
                "test",
                client=client,
                enable_thinking=True,
                reasoning_budget_tokens=512,
                max_output_tokens=1280,
                history_turns=1,
            )
            first = await policy.decide("economy", {}, [])
            second = await policy.decide("economy", {}, [])
            for request in requests:
                assert request["chat_template_kwargs"] == {"enable_thinking": True}
                assert request["reasoning_format"] == "deepseek"
                assert request["reasoning_budget_tokens"] == 512
                assert request["max_tokens"] == 1280
                assert request["response_format"]["type"] == "json_schema"
            assert first["inference_settings"] == {
                "temperature": requests[0]["temperature"],
                "enable_thinking": True,
                "reasoning_format": "deepseek",
                "reasoning_budget_tokens": 512,
                "max_output_tokens": 1280,
                "history_turns": 1,
                "strict_schema": True,
                "context_window_tokens": None,
                "context_window_source": "unknown",
            }
            assert first["provider_diagnostics"] == {"has_reasoning_content": True, "finish_reason": "stop"}
            assert first["usage"] == {"input_tokens": 100, "output_tokens": 550}
            assert policy.accounting["economy"]["output_tokens"] == 1100
            assert first["request_count"] == second["request_count"] == 1
            persisted = json.dumps([first, second, policy.states["economy"].history, requests[1]])
            assert secret_field not in persisted and secret_inline not in persisted
            profile = policy.inference_settings
            profile["enable_thinking"] = False
            assert policy.inference_settings["enable_thinking"] is True

    asyncio.run(run())


@pytest.mark.parametrize("thinking", [False, None])
def test_default_and_unset_thinking_preserve_compatible_request_and_safe_diagnostics(thinking):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(decision())}, "finish_reason": {"x": 1}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, enable_thinking=thinking)
            out = await policy.decide("economy", {}, [])
            request = requests[0]
            if thinking is False:
                assert request["chat_template_kwargs"] == {"enable_thinking": False}
            else:
                assert "chat_template_kwargs" not in request
            assert "reasoning_format" not in request and "reasoning_budget_tokens" not in request
            assert request["max_tokens"] == 768
            assert out["inference_settings"]["history_turns"] == 3
            assert out["inference_settings"]["reasoning_budget_tokens"] is None
            assert out["provider_diagnostics"] == {"has_reasoning_content": False, "finish_reason": None}
            assert out["request_count"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("budget", [0, -1, 1025, True, None, "512", float("inf")])
def test_reasoning_budget_rejects_unbounded_or_non_integer_values_without_network(budget):
    with pytest.raises(ValueError, match="reasoning_budget_tokens"):
        LLMPolicy("http://localhost/v1", "test", enable_thinking=True, reasoning_budget_tokens=budget)


def test_thinking_budget_must_leave_room_for_the_public_json_decision_without_network():
    with pytest.raises(ValueError, match="leave output tokens"):
        LLMPolicy("http://localhost/v1", "test", enable_thinking=True, reasoning_budget_tokens=768)
    with pytest.raises(ValueError, match="boolean"):
        LLMPolicy("http://localhost/v1", "test", enable_thinking="true")


def truncated_provider_response(prompt_tokens=8000, completion_tokens=192):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": None, "reasoning_content": "PRIVATE_TRUNCATED_THOUGHT"},
                     "finish_reason": "length"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


def test_length_at_configured_context_capacity_trims_history_with_safe_attempt_evidence():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return truncated_provider_response() if len(requests) == 1 else response(decision())

        original = saved_policy_history(2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, context_window_tokens=8192)
            policy.states["economy"].history = json.loads(json.dumps(original))
            out = await policy.decide("economy", {"minerals": 321}, [])
            assert out["request_count"] == policy.accounting["economy"]["calls"] == 2
            assert requests[1]["messages"] == [requests[0]["messages"][0], *original[2:], requests[0]["messages"][-1]]
            assert out["usage"] == {"input_tokens": 8012, "output_tokens": 200}
            assert out["usage_reported"] is True
            assert out["context_recovery"]["headroom_errors"] == 1
            assert out["context_recovery"]["recovery_attempts"] == 1
            assert out["attempt_diagnostics"] == [
                {"attempt": 1, "http_status": 200, "finish_reason": "length", "prompt_tokens": 8000,
                 "completion_tokens": 192, "has_reasoning_content": True},
                {"attempt": 2, "http_status": 200, "finish_reason": None, "prompt_tokens": 12,
                 "completion_tokens": 8, "has_reasoning_content": False},
            ]
            assert out["inference_settings"]["context_window_tokens"] == 8192
            assert out["inference_settings"]["context_window_source"] == "configured"
            assert "PRIVATE_TRUNCATED_THOUGHT" not in json.dumps([out, policy.states["economy"].history])

    asyncio.run(run())


@pytest.mark.parametrize("history_pairs,max_attempts,expected_calls", [(0, 3, 1), (1, 3, 2), (3, 3, 3)])
def test_unrecoverable_output_headroom_is_bounded_and_preserves_every_reported_attempt(
    history_pairs, max_attempts, expected_calls
):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return truncated_provider_response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, max_attempts=max_attempts,
                               context_window_tokens=8192)
            policy.states["economy"].history = saved_policy_history(history_pairs)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            error = caught.value
            assert error.code == "context_headroom_exhausted"
            assert len(requests) == error.request_count == expected_calls
            assert len(error.attempt_diagnostics) == expected_calls
            assert error.usage == {"input_tokens": 8000 * expected_calls, "output_tokens": 192 * expected_calls}
            assert error.usage_reported is True
            assert error.context_recovery["headroom_errors"] == expected_calls
            assert error.provider_diagnostics == {"has_reasoning_content": True, "finish_reason": "length"}
            assert error.inference_settings["context_window_tokens"] == 8192
            assert all(not any("previous output" in message["content"] for message in request["messages"])
                       for request in requests)
            assert "PRIVATE_TRUNCATED_THOUGHT" not in str(vars(error))

    asyncio.run(run())


@pytest.mark.parametrize("context,prompt,completion", [
    (None, 8000, 192), (16384, 8000, 192), (8192, 4000, 768), (8192, True, "192"),
])
def test_length_without_known_capacity_evidence_is_not_guessed_as_context_failure(context, prompt, completion):
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return truncated_provider_response(prompt, completion)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, context_window_tokens=context)
            history = saved_policy_history(2)
            policy.states["economy"].history = history.copy()
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert len(requests) == caught.value.request_count == 1
            assert caught.value.code == "output_truncated"
            assert caught.value.context_recovery["headroom_errors"] == 0
            assert policy.states["economy"].history == history
            if isinstance(prompt, bool):
                assert caught.value.usage == {"input_tokens": None, "output_tokens": None}
                assert caught.value.attempt_diagnostics[0]["prompt_tokens"] is None

    asyncio.run(run())


def test_headroom_recovery_keeps_the_original_deadline_and_unknown_timeout_usage():
    async def run():
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return truncated_provider_response()
            await asyncio.sleep(.5)
            return response(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client, timeout=.05,
                               context_window_tokens=8192, max_attempts=3)
            policy.states["economy"].history = saved_policy_history(2)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert caught.value.code == "timeout" and caught.value.request_count == 2
            assert caught.value.latency_ms < 300
            assert caught.value.usage == {"input_tokens": None, "output_tokens": None}
            assert caught.value.attempt_diagnostics[-1] == {
                "attempt": 2, "http_status": None, "finish_reason": None, "prompt_tokens": None,
                "completion_tokens": None, "has_reasoning_content": None,
            }

    asyncio.run(run())


def test_http_error_attempt_records_only_safe_status_and_unknown_usage():
    async def run():
        def handler(request):
            return httpx.Response(400, json={"error": "PRIVATE_PROVIDER_BODY_NOT_FOR_TRACE"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            with pytest.raises(PolicyError) as caught:
                await policy.decide("economy", {}, [])
            assert caught.value.attempt_diagnostics == [{
                "attempt": 1, "http_status": 400, "finish_reason": None, "prompt_tokens": None,
                "completion_tokens": None, "has_reasoning_content": None,
            }]
            assert caught.value.usage == {"input_tokens": None, "output_tokens": None}
            assert "PRIVATE_PROVIDER_BODY_NOT_FOR_TRACE" not in str(vars(caught.value))

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, -1, True, "8192", 8192.0])
def test_context_window_configuration_must_be_an_explicit_positive_integer(value):
    with pytest.raises(ValueError, match="context_window_tokens"):
        LLMPolicy("http://localhost/v1", "test", context_window_tokens=value)


def test_observed_queue_supply_and_known_destination_guidance_does_not_select_tactical_actions():
    async def run():
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response({**decision(), "actions": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = LLMPolicy("http://localhost/v1", "test", client=client)
            await policy.decide("tactics", {"task": {"scenario": "full_match"}, "army_count": 14,
                                           "queued_counts": {"MARINE": 1}}, [])
            tactics = requests[-1]["messages"][0]["content"]
            assert "continuously producing queue to become empty" in tactics
            assert "observable release condition" in tactics
            assert "prior location usable without current vision" in tactics
            assert "scout moves only one eligible unit" in tactics
            await policy.decide("economy", {"supply_used": 23, "supply_cap": 23}, [])
            economy = requests[-1]["messages"][0]["content"]
            assert "native team blockers" in economy
            assert "supply_used/supply_cap" in economy
            assert "available slots, not demand" in economy

    asyncio.run(run())

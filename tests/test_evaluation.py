"""Mission evaluation uses observations, not command receipts or model claims."""
from copy import deepcopy

import pytest

from sc2_harness.evaluation import evaluate_mission, milestones, outcome
from sc2_harness.missions import get_mission, get_role_task, mission_catalog


def observation(**changes):
    obs = {
        "engine": "sc2", "game_time": 30, "workers": 14, "supply_cap": 23,
        "structures": {"SUPPLYDEPOT": 1, "BARRACKS": 1},
        "unit_counts": {"SCV": 14, "MARINE": 0}, "units": [], "score": {"collected_vespene": 0},
        "map": {"start": [10, 10], "expansions": [[10, 10]]},
        "upgrades": {"completed": [], "in_progress": []}, "scouted": False, "result": None,
    }
    obs.update(changes)
    return obs


def base(x, y, kind="COMMANDCENTER", ready=True, owner="self"):
    return {"type": kind, "owner": owner, "is_ready": ready, "x": x, "y": y}


def item(evaluation, key):
    return next(part for part in evaluation["milestones"] if part["id"] == key)


def logistics(**changes):
    obs = observation(workers=24, structures={"SUPPLYDEPOT": 1, "BARRACKS": 1, "REFINERY": 2},
                      units=[base(10, 10), base(30, 10)], score={"collected_vespene": 200},
                      map={"start": [10, 10], "expansions": [[10, 10], [30, 10]]})
    obs.update(changes)
    return obs


def army(**changes):
    obs = observation(structures={"SUPPLYDEPOT": 1, "BARRACKS": 2, "ENGINEERINGBAY": 1},
                      supply_cap=31,
                      unit_counts={"MARINE": 12}, score={"collected_vespene": 100},
                      upgrades={"completed": ["TERRANINFANTRYWEAPONSLEVEL1"], "in_progress": []})
    obs.update(changes)
    return obs


def test_catalog_is_detached_and_stages_have_policy_goals_and_valid_dependencies():
    catalog = mission_catalog()
    assert {mission["id"] for mission in catalog} == {
        "build_and_scout", "full_match", "expansion_logistics", "army_readiness", "expansion_and_victory",
    }
    for mission in catalog:
        expected_map = "AbyssalReefLE" if mission["id"] in {
            "expansion_logistics", "army_readiness", "expansion_and_victory",
        } else "Simple64"
        assert mission["suggested_map"] == expected_map
        seen, criteria = set(), set()
        for stage in mission["stages"]:
            assert set(stage["prerequisites"]) <= seen
            assert stage["policy_goal"].isascii()
            seen.add(stage["id"])
            for criterion in stage["criteria"]:
                assert criterion["id"] not in criteria and criterion["target"] > 0
                criteria.add(criterion["id"])
        assert 1 <= mission["suggested_budget"]["max_rounds"] <= 500
        assert 10 <= mission["suggested_budget"]["max_game_seconds"] <= 3600
    catalog[0]["stages"][0]["criteria"].clear()
    assert len(get_mission("build_and_scout")["stages"][0]["criteria"]) == 5
    with pytest.raises(ValueError, match="unknown mission"):
        get_mission("invented")


def test_legacy_default_milestones_latch_first_observed_time_and_reported():
    obs = observation(unit_counts={"MARINE": 4}, scouted=True)
    previous = milestones(obs)
    assert outcome("build_and_scout", obs, previous) == "success"
    previous[0]["reported"] = True
    after_loss = milestones(observation(game_time=90, workers=2, unit_counts={"MARINE": 0}), previous)
    assert all(part["achieved"] for part in after_loss)
    assert after_loss[0]["value"] == 2 and after_loss[0]["reported"] is True
    assert {part["achieved_at_game_time"] for part in after_loss} == {30}


@pytest.mark.parametrize("scenario", [mission["id"] for mission in mission_catalog()])
def test_defeat_always_failure_even_after_all_thresholds(scenario):
    obs = army(workers=24, structures={"SUPPLYDEPOT": 1, "BARRACKS": 2, "ENGINEERINGBAY": 1, "REFINERY": 2},
               units=[base(10, 10), base(40, 10)], score={"collected_vespene": 200}, scouted=True,
               map={"start": [10, 10], "expansions": [[10, 10], [40, 10]]},
               result="Victory")
    previous = evaluate_mission(scenario, obs)
    result = evaluate_mission(scenario, {**obs, "result": "Defeat"}, previous, terminated=True)
    assert result["outcome"] == "failure" and result["engine_result"] == "Defeat"
    assert result["censored"] is False


def test_stage_dependencies_prevent_premature_latching_and_keep_actual_current_values():
    first = evaluate_mission("army_readiness", army(structures={"SUPPLYDEPOT": 1}))
    assert first["stages"][0]["status"] == "active"
    assert first["stages"][1]["status"] == first["stages"][2]["status"] == "blocked"
    assert item(first, "marines_12")["value"] == 12
    assert item(first, "marines_12")["eligible"] is False
    assert item(first, "marines_12")["achieved"] is False
    second = evaluate_mission("army_readiness", army(game_time=60, unit_counts={"MARINE": 2}), first)
    assert second["stage_completion"] == {"completed": 2, "total": 3}
    assert second["current_stage"]["id"] == "upgraded_army"
    assert item(second, "marines_12")["achieved"] is False
    assert item(second, "infantry_weapons_1")["achieved_at_game_time"] == 60


def test_logistics_completes_only_from_complete_distinct_bases_and_cumulative_gas():
    result = evaluate_mission("expansion_logistics", logistics(vespene=0))
    assert result["outcome"] == "success" and result["complete"] is True
    assert result["progress"] == 1 and result["current_stage"] is None
    assert result["stage_completion"] == {"completed": 3, "total": 3}
    assert all(stage["achieved_at_game_time"] == 30 for stage in result["stages"])
    assert result["engine_result"] is None


@pytest.mark.parametrize("units,expected", [
    ([base(10, 10), base(20, 10)], 1),
    ([base(10, 10), base(22, 10)], 2),
    ([base(10, 10), base(30, 10, ready=False)], 1),
    ([base(10, 10), base(30, 10, owner="enemy")], 1),
    ([base(10, 10, "ORBITALCOMMAND"), base(30, 10, "PLANETARYFORTRESS")], 2),
    ([base(10, 10), {"type": "COMMANDCENTER", "owner": "self", "is_ready": True}], None),
    (None, None),
])
def test_expansion_requires_observed_ready_friendly_positions(units, expected):
    result = evaluate_mission("expansion_logistics", logistics(units=units))
    criterion = item(result, "expanded_bases")
    assert criterion["value"] == expected
    assert criterion["known"] is (expected is not None)
    assert criterion["achieved"] is (expected == 2)
    assert result["outcome"] == ("success" if expected == 2 else None)


@pytest.mark.parametrize("score", [{}, None, {"collected_vespene": None}, {"collected_vespene": float("nan")}])
def test_missing_or_invalid_cumulative_score_never_uses_current_gas_inventory(score):
    result = evaluate_mission("expansion_logistics", logistics(score=score, vespene=999))
    assert item(result, "gas_200")["known"] is False
    assert item(result, "gas_200")["value"] is None
    assert result["outcome"] is None


def test_units_are_simultaneous_not_summed_or_counted_from_commands_and_queues():
    obs = army(unit_counts={"MARINE": 6}, pending_counts={"MARINE": 20}, queued_counts={"MARINE": 20},
               accepted=[{"tool": "train_marine"}], model_claim="12 Marines complete")
    first = evaluate_mission("army_readiness", obs)
    second = evaluate_mission("army_readiness", {**obs, "game_time": 90}, first)
    assert item(second, "marines_12")["value"] == 6
    assert item(second, "marines_12")["achieved"] is False
    assert second["outcome"] is None


def test_ready_counts_override_all_counts_and_pending_buildings():
    result = evaluate_mission("army_readiness", army(ready_counts={"SCV": 14, "SUPPLYDEPOT": 1, "BARRACKS": 1}))
    assert item(result, "barracks_2")["value"] == 1
    assert item(result, "marines_12")["value"] == 0
    assert result["outcome"] is None


@pytest.mark.parametrize("upgrades", [None, {}, {"completed": [], "in_progress": [
    {"upgrade": "TERRANINFANTRYWEAPONSLEVEL1", "progress": 0.99},
]}])
def test_research_in_progress_or_accepted_does_not_complete_upgrade(upgrades):
    result = evaluate_mission("army_readiness", army(upgrades=upgrades, research_accepted=True))
    assert item(result, "infantry_weapons_1")["achieved"] is False
    assert result["outcome"] is None


def test_army_task_observed_upgrade_and_army_latch_with_distinct_first_times():
    first = evaluate_mission("army_readiness", army(unit_counts={"MARINE": 6}))
    assert item(first, "infantry_weapons_1")["achieved_at_game_time"] == 30
    second = evaluate_mission("army_readiness", army(game_time=100), first)
    assert second["outcome"] == "success" and second["stages"][-1]["achieved_at_game_time"] == 100
    lost = evaluate_mission("army_readiness", observation(game_time=150), second)
    assert lost["outcome"] == "success"
    assert item(lost, "marines_12")["value"] == 0
    assert item(lost, "marines_12")["achieved_at_game_time"] == 100


def test_army_never_combines_pre_upgrade_marines_with_later_upgrade_after_they_died():
    first = evaluate_mission("army_readiness", army(upgrades={"completed": []}))
    assert item(first, "marines_12")["value"] == 0
    assert item(first, "marines_12")["achieved"] is False
    second = evaluate_mission("army_readiness", army(game_time=100, unit_counts={"MARINE": 0}), first)
    assert item(second, "infantry_weapons_1")["achieved"] is True
    assert item(second, "marines_12")["achieved"] is False and second["outcome"] is None
    third = evaluate_mission("army_readiness", army(game_time=150), second)
    assert third["outcome"] == "success"
    assert item(third, "marines_12")["achieved_at_game_time"] == 150
    projection = get_role_task("production", second)
    marine_goal = next(gap for gap in projection["goal_gaps"] if gap["id"] == "marines_12")
    assert marine_goal["requires_upgrade"] == "TERRANINFANTRYWEAPONSLEVEL1"


@pytest.mark.parametrize("upgrades", [None, {}, {"in_progress": []}])
def test_missing_upgrade_evidence_makes_upgraded_marine_count_unknown(upgrades):
    result = evaluate_mission("army_readiness", army(upgrades=upgrades))
    assert item(result, "marines_12")["value"] is None
    assert item(result, "marines_12")["known"] is False


@pytest.mark.parametrize("capacity,achieved", [(23, False), (29, False), (30, True), (31, True), (None, False)])
def test_army_final_stage_keeps_economy_active_until_observed_supply_capacity(capacity, achieved):
    result = evaluate_mission("army_readiness", army(supply_cap=capacity, unit_counts={"MARINE": 6}))
    criterion = item(result, "army_supply_capacity")
    assert criterion["achieved"] is achieved
    assert criterion["known"] is (capacity is not None)
    assert result["current_stage"]["id"] == "upgraded_army"
    role = get_role_task("economy", result)
    assert role["role_done"] is achieved
    assert bool(role["goal_gaps"]) is not achieved
    if not achieved:
        assert role["goal_gaps"][0]["metric"] == "supply_cap"


def test_supply_loss_reactivates_economy_without_erasing_historical_milestone():
    first = evaluate_mission("army_readiness", army(unit_counts={"MARINE": 6}, supply_cap=31))
    assert get_role_task("economy", first)["role_done"] is True
    after_loss = evaluate_mission("army_readiness", army(game_time=90, unit_counts={"MARINE": 6}, supply_cap=23), first)
    historical = item(after_loss, "army_supply_capacity")
    assert historical["achieved"] is True and historical["achieved_at_game_time"] == 30
    assert historical["value"] == 23 and after_loss["current_stage"]["id"] == "upgraded_army"
    economy = get_role_task("economy", after_loss)
    assert economy["role_done"] is False
    assert len(economy["goal_gaps"]) == 1
    assert economy["goal_gaps"][0]["remaining"] == 7
    assert economy["goal_gaps"][0]["maintain"] is True
    assert economy["role_milestones"][0]["done"] is False


def test_non_maintenance_goals_stay_latched_in_role_projection_after_losses():
    first = evaluate_mission("build_and_scout", observation(scouted=True))
    after_loss = evaluate_mission("build_and_scout", observation(game_time=60, workers=2, scouted=False), first)
    assert after_loss["current_stage"] is not None  # Marines still missing, so the mission is active.
    assert get_role_task("tactics", after_loss)["role_done"] is True
    assert get_role_task("economy", after_loss)["role_done"] is True


@pytest.mark.parametrize("world,expected", [
    ({"start": [10, 10], "expansions": [[10, 10], [26, 10]]}, 0),
    ({"start": [10, 10], "expansions": [[10, 10], [26.1, 10]]}, 1),
    ({"start": [10, 10], "expansions": [[10, 10], [30, 10], [30, 10]]}, 1),
    ({"start": [10, 10], "expansions": []}, 0),
    ({"start": [10, 10]}, None),
    ({"expansions": [[30, 10]]}, None),
    ({"start": [None, 10], "expansions": [[30, 10]]}, None),
    ({"start": [10, 10], "expansions": [[None, 10]]}, None),
    (None, None),
])
def test_expansion_discovery_is_observed_and_relative_to_original_start(world, expected):
    result = evaluate_mission("expansion_logistics", logistics(map=world))
    criterion = item(result, "known_expansion_site")
    assert criterion["value"] == expected and criterion["known"] is (expected is not None)
    assert criterion["achieved"] is (expected == 1)
    assert result["outcome"] == ("success" if expected == 1 else None)


def test_expansion_discovery_gives_tactics_a_goal_and_stays_valid_after_base_is_built():
    first = evaluate_mission("expansion_logistics", logistics(map={"start": [10, 10], "expansions": [[10, 10]]}))
    role = get_role_task("tactics", first)
    assert role["role_phase"] == "expansion" and role["role_done"] is False
    assert role["goal_gaps"][0]["id"] == "known_expansion_site"
    assert role["goal_gaps"][0]["min_distance"] == 16
    assert "Explore new resource locations, not only the enemy start" in role["focus"]
    assert "reaching the enemy start alone does not satisfy resource discovery" in role["role_objective"]
    # A completed base is already at the discovered site in this same observation.
    second = evaluate_mission("expansion_logistics", logistics(game_time=100), first)
    assert item(second, "known_expansion_site")["achieved_at_game_time"] == 100
    assert second["outcome"] == "success"
    assert second["mission_version"] == 2
    assert get_mission("expansion_and_victory")["version"] == get_mission("army_readiness")["version"] == 2


def test_expansion_victory_needs_both_prerequisites_and_native_win():
    first = evaluate_mission("expansion_and_victory", logistics())
    assert first["outcome"] is None and first["stage_completion"]["completed"] == 2
    win = evaluate_mission("expansion_and_victory", logistics(result="Victory", game_time=600), first)
    assert win["outcome"] == "success" and win["engine_result"] == "Victory"
    early_win = evaluate_mission("expansion_and_victory", observation(result="Victory"))
    assert early_win["outcome"] == "incomplete" and early_win["engine_result"] == "Victory"
    assert early_win["censored"] is True


@pytest.mark.parametrize("reason", ["game_time_budget", "round_budget", "user_stop", "model_error"])
def test_termination_records_right_censoring_without_inventing_failure_or_reach_times(reason):
    result = evaluate_mission("army_readiness", observation(game_time=300), terminated=True, termination_reason=reason)
    assert result["outcome"] == "incomplete" and result["censored"] is True
    assert result["termination_reason"] == reason
    assert result["stages"][0]["complete"] is True
    assert result["stages"][0]["censored"] is False
    for criterion in result["milestones"]:
        if not criterion["achieved"]:
            assert criterion["achieved_at_game_time"] is None and criterion["censored"] is True
    assert result["engine_result"] is None


def test_role_projection_is_current_stage_only_with_unknown_gaps_and_no_actions():
    result = evaluate_mission("expansion_logistics", logistics(score=None, structures={"SUPPLYDEPOT": 1, "BARRACKS": 1}))
    economy = get_role_task("economy", result, {})
    assert economy["role_phase"] == "gas_logistics" and economy["role_done"] is False
    assert "objective" not in economy and economy["role_objective"].isascii()
    gas = next(gap for gap in economy["goal_gaps"] if gap["id"] == "gas_200")
    assert gas["observed"] is None and gas["remaining"] is None
    refinery = next(gap for gap in economy["goal_gaps"] if gap["id"] == "refineries_2")
    assert refinery["types"] == ["REFINERY"]
    assert "You are the economy role" in economy["role_objective"]
    assert "actions=[] is valid" in economy["role_objective"]
    assert "actions" not in economy
    idle = get_role_task("tactics", result)
    assert idle["role_done"] is True and idle["goal_gaps"] == []
    with pytest.raises(ValueError, match="unknown role"):
        get_role_task("enemy", result)


def test_role_specific_full_match_duties_and_upgrade_metadata():
    complete_game = evaluate_mission("full_match", observation())
    for role in ("economy", "production", "tactics"):
        projection = get_role_task(role, complete_game)
        assert f"You are the {role} role" in projection["role_objective"]
        assert projection["role_done"] is False
        assert projection["continuous_role_duty"] is True
        assert "native Victory goal is still pending" in projection["role_objective"]
        assert "Entering this phase does not complete the task" in projection["role_objective"]
    assert "forming and replacing a combat army" in get_role_task("production", complete_game)["role_objective"]
    readiness = evaluate_mission("army_readiness", army(upgrades={"completed": []}))
    projection = get_role_task("production", readiness)
    upgrade = next(gap for gap in projection["goal_gaps"] if gap["id"] == "infantry_weapons_1")
    assert upgrade["upgrade"] == "TERRANINFANTRYWEAPONSLEVEL1"
    assert "TERRANINFANTRYWEAPONSLEVEL1" in projection["role_objective"]


def test_previous_from_another_scenario_cannot_leak_latched_progress():
    previous = evaluate_mission("build_and_scout", observation(workers=100, scouted=True, unit_counts={"MARINE": 50}))
    before = deepcopy(previous)
    result = evaluate_mission("army_readiness", observation(workers=0, structures={}), previous)
    assert not any(part["achieved"] for part in result["milestones"])
    assert previous == before


def test_full_match_does_not_treat_infrastructure_as_victory_and_preserves_tie():
    assert evaluate_mission("full_match", logistics())["outcome"] is None
    result = evaluate_mission("full_match", observation(result="Tie"))
    assert result["outcome"] == "incomplete" and result["engine_result"] == "Tie"
    assert outcome("full_match", observation(result="Victory"), []) == "success"


@pytest.mark.parametrize("scenario", ["full_match", "expansion_and_victory"])
def test_sandbox_terminal_claim_is_not_a_native_victory(scenario):
    result = evaluate_mission(scenario, logistics(engine="sandbox", result="Victory"))
    assert item(result, "native_victory")["achieved"] is False
    assert result["outcome"] == "incomplete"

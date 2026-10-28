"""Reference-strategy tests use synthetic game states and policies, never SC2/HTTP."""

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from sc2_harness.evaluation import evaluate_mission
from sc2_harness.guidance import REFERENCE_PROFILE, reference_plan
from sc2_harness.missions import get_role_task
from sc2_harness.models import ROLES, RunConfig
from sc2_harness.runtime import RunManager
from sc2_harness.settings import Settings


def observed(marines=0, *, time=0, result=None, engine="sc2"):
    return {"engine": engine, "game_loop": int(time * 22.4), "game_time": time,
            "player_id": 1, "ready_counts": {"SCV": 20, "SUPPLYDEPOT": 1, "BARRACKS": 2, "MARINE": marines},
            "pending_counts": {}, "queued_counts": {},
            "structures": {"COMMANDCENTER": 1, "SUPPLYDEPOT": 1, "BARRACKS": 2},
            "unit_counts": {"SCV": 20, "MARINE": marines}, "workers": 20,
            "army_count": marines,
            "supply_used": 20 + marines, "supply_cap": 60, "minerals": 500, "vespene": 100,
            "units": [], "result": result, "map": {"start": [10, 10], "enemy_start": [80, 80]}}


@pytest.mark.parametrize("changes", [
    {"policy": "scripted"}, {"scenario": "army_readiness"}, {"execution_guidance": False},
    {"guidance_profile": "silent_auto_strategy"},
])
def test_reference_profile_rejects_incompatible_or_unknown_configurations(changes):
    values = {"policy": "llm", "scenario": "full_match", "execution_guidance": True,
              "guidance_profile": REFERENCE_PROFILE}
    with pytest.raises(ValidationError):
        RunConfig(**(values | changes))


def test_default_profile_remains_none_and_does_not_inject_a_plan_or_formal_goals():
    assert RunConfig().guidance_profile == "none"
    state = observed(12)
    original = deepcopy(state)
    evaluation = evaluate_mission("full_match", state)
    assert reference_plan("none", state, {"profile": REFERENCE_PROFILE}) is None
    for role in ROLES:
        task = get_role_task(role, evaluation, state)
        assert "reference_plan" not in task
        assert task["role_done"] is False
    assert [item["metric"] for item in evaluation["milestones"]] == ["native_victory"]
    assert state == original


def test_reference_count_latches_a_recommendation_not_an_issued_attack_or_assembled_force():
    below = reference_plan(REFERENCE_PROFILE, observed(11, time=10))
    reached = reference_plan(REFERENCE_PROFILE, observed(12, time=20), below)
    before = deepcopy(reached)
    lost_units = reference_plan(REFERENCE_PROFILE, observed(3, time=30), reached)
    assert below["phase"] == "form_reference_force"
    assert reached["phase"] == lost_units["phase"] == "recommended_assault_phase"
    assert reached["reference_size_reached_at_game_time"] == lost_units["reference_size_reached_at_game_time"] == 20
    assert lost_units["phase_entered_game_time"] == 20
    assert next(item for item in lost_units["soft_targets"] if item["id"] == "ready_marines")["met"] is False
    assert "not_success_or_issued_actions" in lost_units["scope"]
    assert not any(key in lost_units for key in ("actions", "attack_started", "assembled", "success"))
    assert reached == before


def test_unknown_or_queued_marines_do_not_prove_reference_size_and_profiles_do_not_share_latches():
    unknown = {"queued_counts": {"MARINE": 99}, "unit_counts": {"MARINE": 99}, "game_time": 20}
    plan = reference_plan(REFERENCE_PROFILE, unknown)
    assert plan["phase"] == "reference_opening"
    plan = reference_plan(REFERENCE_PROFILE, unknown, reference_plan(REFERENCE_PROFILE, observed()))
    count = next(item for item in plan["soft_targets"] if item["id"] == "ready_marines")
    assert count["known"] is False and count["observed"] is count["met"] is count["progress"] is None
    assert plan["phase"] == "form_reference_force"
    foreign = {"profile": "other_profile", "version": 1, "advisory": True,
               "phase": "recommended_assault_phase", "reference_size_reached_at_game_time": 1}
    assert reference_plan(REFERENCE_PROFILE, observed(0), foreign)["phase"] == "form_reference_force"
    with pytest.raises(ValueError, match="Unknown guidance profile"):
        reference_plan("unsupported", observed())


def test_worker_reference_uses_native_worker_counter_before_partial_public_scv_count():
    observation = observed()
    observation["workers"], observation["ready_counts"]["SCV"] = 22, 20
    plan = reference_plan(REFERENCE_PROFILE, observation)
    workers = next(item for item in plan["soft_targets"] if item["id"] == "workers")
    assert workers["observed"] == 22 and workers["observed_source"] == "observation.workers"
    del observation["workers"]
    workers = reference_plan(REFERENCE_PROFILE, observation)["soft_targets"][0]
    assert workers["observed"] == 20 and workers["observed_source"] == "ready_counts.SCV"


def test_opening_assigns_only_owned_targets_and_pending_barracks_is_not_ready():
    observation = observed()
    observation["workers"] = 14
    observation["ready_counts"] = {"SCV": 14, "SUPPLYDEPOTLOWERED": 1}
    observation["pending_counts"] = {"BARRACKS": 1}
    observation["structures"] = {"COMMANDCENTER": 1, "SUPPLYDEPOTLOWERED": 1}
    original = deepcopy(observation)
    plan = reference_plan(REFERENCE_PROFILE, observation)
    global_plan = deepcopy(plan)
    assert plan["phase"] == "reference_opening" and plan["revision"] == 3
    evaluation = evaluate_mission("full_match", observation)
    economy = get_role_task("economy", evaluation, observation, reference_plan=plan)["reference_plan"]
    production = get_role_task("production", evaluation, observation, reference_plan=plan)["reference_plan"]
    tactics_task = get_role_task("tactics", evaluation, observation, reference_plan=plan)
    assert {target["tool"] for target in economy["role_targets"]} == {"train_worker", "build_supply"}
    assert economy["role_progress"]["met_targets"] == economy["role_progress"]["assigned_targets"] == 2
    barracks = production["role_targets"][0]
    assert barracks["tool"] == "build_barracks" and barracks["observed"] == 0
    assert barracks["gap"] == 1 and barracks["met"] is False
    assert barracks["in_progress"] == {"types": ["BARRACKS"], "pending": 1, "queued": 0}
    assert tactics_task["reference_plan"]["role_targets"] == [] and tactics_task["role_done"] is False
    assert "no mandatory opening scout" in tactics_task["reference_plan"]["focus"]
    assert "do not send an SCV" in tactics_task["reference_plan"]["focus"]
    assert "actions=[] can leave minerals for production" in economy["focus"]
    assert "Pending jobs are already paid" in production["focus"]
    for role, projected in (("economy", economy), ("production", production),
                            ("tactics", tactics_task["reference_plan"])):
        assert "soft_targets" not in projected
        assert all(target["role"] == role for target in projected["role_targets"])
        assert projected["role_targets"] == [target for target in plan["soft_targets"] if target["role"] == role]
    assert plan == global_plan
    assert observation == original


def test_observed_opening_latches_formation_but_not_across_reference_revisions():
    opening = observed()
    opening["workers"] = 12
    opening["ready_counts"] = {"SCV": 12}
    opening["pending_counts"] = {"SUPPLYDEPOT": 1, "BARRACKS": 1}
    opening["queued_counts"] = {"SCV": 2}
    opening["structures"] = {"COMMANDCENTER": 1}
    initial = reference_plan(REFERENCE_PROFILE, opening)
    assert initial["phase"] == "reference_opening"
    assert next(t for t in initial["soft_targets"] if t["id"] == "workers")["gap"] == 2
    formed = reference_plan(REFERENCE_PROFILE, observed(), initial)
    assert formed["phase"] == "form_reference_force"
    damaged = reference_plan(REFERENCE_PROFILE, opening, formed)
    assert damaged["phase"] == "form_reference_force"
    old_revision = deepcopy(formed)
    old_revision["revision"] = 2
    assert reference_plan(REFERENCE_PROFILE, opening, old_revision)["phase"] == "reference_opening"
    del old_revision["revision"]
    assert reference_plan(REFERENCE_PROFILE, opening, old_revision)["phase"] == "reference_opening"


def test_twelve_ready_marines_recommend_assault_even_with_all_other_setup_targets_unmet():
    observation = observed(12)
    observation["workers"] = 1
    observation["ready_counts"] = {"SCV": 1, "MARINE": 12}
    observation["structures"] = {"COMMANDCENTER": 1}
    observation["supply_used"], observation["supply_cap"] = 13, 13
    plan = reference_plan(REFERENCE_PROFILE, observation)
    assert plan["phase"] == "recommended_assault_phase"
    assert {target["id"] for target in plan["soft_targets"] if target["met"]} == {"ready_marines"}
    evaluation = evaluate_mission("full_match", observation)
    for role in ROLES:
        task = get_role_task(role, evaluation, observation, reference_plan=plan)
        assert task["role_done"] is False
        assert [target["metric"] for target in task["goal_gaps"]] == ["native_victory"]
    assert evaluation["complete"] is False


def positioned_army(total, near):
    observation = observed(total)
    observation["units"] = [
        {"tag": index + 100, "type": "MARINE", "owner": "self", "is_structure": False,
         "is_ready": True, "x": 80 + index * .1 if index < near else 10 + index * .1,
         "y": 80 if index < near else 10}
        for index in range(total)
    ]
    return observation


def test_production_setup_met_preserves_continuous_idle_barracks_training_duty():
    observation = observed(13)
    observation["ready_counts"]["BARRACKS"] = 4
    observation["production"] = [
        {"tag": index, "type": "BARRACKS", "is_ready": True, "is_busy": False, "orders": []}
        for index in range(4)
    ]
    observation["action_state"] = {"train_marine": {"available": True}}
    before = deepcopy(observation)
    plan = reference_plan(REFERENCE_PROFILE, observation)
    task = get_role_task("production", evaluate_mission("full_match", observation), observation, reference_plan=plan)
    role = task["reference_plan"]
    assert role["role_progress"]["met_targets"] == role["role_progress"]["assigned_targets"] == 2
    assert role["continuous_duty"] == {
        "owner": "production", "activity": "train_affordable_marines_from_idle_barracks",
        "continues_after_setup": True, "until": "native_terminal",
        "progress": {"ready_marines": 13, "queued_marines": 0,
                     "idle_ready_barracks": 4, "train_marine_available": True},
    }
    assert "never a production quota or cap" in role["focus"]
    assert "Keep training affordable Marines" in role["focus"]
    assert "continuous_duties" not in role and "initial_hint_public_progress" not in role
    assert task["role_done"] is False and "actions" not in role
    assert set(plan["continuous_duties"]) == set(ROLES) and observation == before


def test_one_scout_or_lone_marine_near_hint_does_not_prove_main_army_arrival():
    state = positioned_army(5, 1)
    state["units"].append({"tag": 999, "type": "SCV", "owner": "self", "is_structure": False,
                           "is_ready": True, "x": 80, "y": 80})
    for observation in (state, positioned_army(1, 1)):
        progress = reference_plan(REFERENCE_PROFILE, observation)["initial_hint_public_progress"]
        assert progress["positions_complete"] is True and progress["coverage"] == 1
        assert progress["near_count"] == 1 and progress["majority_near"] is False
        assert progress["initial_hint_reached"] is False and progress["reached_at_game_time"] is None


def test_early_two_unit_search_does_not_precomplete_launch_force_progress_but_later_survivors_can():
    early = reference_plan(REFERENCE_PROFILE, positioned_army(2, 2))
    assert early["phase"] == "form_reference_force"
    assert early["initial_hint_public_progress"]["majority_near"] is True
    assert early["initial_hint_public_progress"]["initial_hint_reached"] is False
    launched = reference_plan(REFERENCE_PROFILE, positioned_army(12, 0), early)
    assert launched["phase"] == "recommended_assault_phase"
    assert launched["initial_hint_public_progress"]["initial_hint_reached"] is False
    survivors = positioned_army(2, 2)
    survivors["game_time"] = 30
    reached = reference_plan(REFERENCE_PROFILE, survivors, launched)
    assert reached["phase"] == "recommended_assault_phase"
    assert reached["initial_hint_public_progress"]["initial_hint_reached"] is True
    assert reached["initial_hint_public_progress"]["reached_at_game_time"] == 30


def test_only_a_known_strict_majority_latches_advisory_hint_progress_then_keeps_history_after_losses():
    half = reference_plan(REFERENCE_PROFILE, positioned_army(12, 6))
    assert half["initial_hint_public_progress"]["majority_near"] is False
    observation = positioned_army(12, 7)
    observation["game_time"] = 100
    before = deepcopy(observation)
    reached = reference_plan(REFERENCE_PROFILE, observation, half)
    evidence = reached["initial_hint_public_progress"]
    assert evidence["army_ready_total"] == evidence["known_total"] == 12
    assert evidence["near_count"] == 7 and evidence["initial_hint_reached"] is True
    assert evidence["reached_at_game_time"] == 100
    lost = positioned_army(3, 0)
    lost["game_time"] = 110
    retained = reference_plan(REFERENCE_PROFILE, lost, reached)
    assert retained["initial_hint_public_progress"]["near_count"] == 0
    assert retained["initial_hint_public_progress"]["majority_near"] is False
    assert retained["initial_hint_public_progress"]["initial_hint_reached"] is True
    assert retained["initial_hint_public_progress"]["reached_at_game_time"] == 100
    task = get_role_task("tactics", evaluate_mission("full_match", lost), lost, reference_plan=retained)
    assert "continue MAIN-ARMY search of other known resource" in task["reference_plan"]["focus"]
    assert task["reference_plan"]["continuous_duty"]["activity"] == "advance_main_army_and_engage_current_targets"
    assert task["role_done"] is False and "not_formal_goal" in evidence["scope"]
    assert observation == before


def test_incomplete_unknown_or_duplicate_army_positions_cannot_manufacture_hint_coverage():
    for defect in ("missing_position", "duplicate_tag", "unknown_total"):
        observation = positioned_army(12, 7)
        if defect == "missing_position":
            del observation["units"][-1]["x"]
        elif defect == "duplicate_tag":
            observation["units"][-1]["tag"] = observation["units"][-2]["tag"]
        else:
            del observation["army_count"]
        progress = reference_plan(REFERENCE_PROFILE, observation)["initial_hint_public_progress"]
        assert progress["positions_complete"] is False and progress["majority_near"] is None
        assert progress["initial_hint_reached"] is False
    unknown = reference_plan(REFERENCE_PROFILE, {})["initial_hint_public_progress"]
    assert unknown["hint_position"] is unknown["army_ready_total"] is unknown["coverage"] is None
    assert unknown["near_count"] is None and unknown["initial_hint_reached"] is False


def test_hint_latch_cannot_cross_reference_revision_or_changed_public_hint():
    previous = reference_plan(REFERENCE_PROFILE, positioned_army(12, 7))
    observation = positioned_army(12, 0)
    changed_revision = deepcopy(previous)
    changed_revision["revision"] = 2
    assert reference_plan(REFERENCE_PROFILE, observation, changed_revision)["initial_hint_public_progress"][
        "initial_hint_reached"] is False
    observation["map"]["enemy_start"] = [120, 120]
    assert reference_plan(REFERENCE_PROFILE, observation, previous)["initial_hint_public_progress"][
        "initial_hint_reached"] is False


def test_soft_targets_met_never_finishes_a_role_or_changes_native_victory_criteria():
    observation = observed(12, time=20)
    evaluation = evaluate_mission("full_match", observation)
    plan = reference_plan(REFERENCE_PROFILE, observation)
    for role in ROLES:
        task = get_role_task(role, evaluation, observation, reference_plan=plan)
        assert task["role_done"] is False and task["continuous_role_duty"] is True
        assert task["reference_plan"]["advisory"] is True
        assert task["reference_plan"]["focus"]
        assert all(item["met"] for item in task["reference_plan"]["role_targets"])
        assert "soft_targets" not in task["reference_plan"]
        assert [gap["metric"] for gap in task["goal_gaps"]] == ["native_victory"]
    assert evaluation["outcome"] is None and evaluation["complete"] is False


def test_reference_production_duty_honors_given_focus_without_changing_default_or_formal_goals():
    observation = observed(12)
    evaluation = evaluate_mission("full_match", observation)
    original = deepcopy(evaluation)
    default = get_role_task("production", evaluation, observation)
    guided = get_role_task("production", evaluation, observation,
                           reference_plan=reference_plan(REFERENCE_PROFILE, observation))
    assert "there is no prescribed army size or build order." in default["role_objective"]
    assert "there is no prescribed army size or build order." not in guided["role_objective"]
    assert "follow the given reference execution focus" in guided["role_objective"]
    assert "setup targets remain advisory, not native Victory requirements" in guided["role_objective"]
    assert guided["role_milestones"] == default["role_milestones"]
    assert guided["goal_gaps"] == default["goal_gaps"]
    assert guided["role_done"] is default["role_done"] is False
    assert evaluation == original


def test_assault_focus_distinguishes_live_target_coordinates_from_search_hints_without_selecting_actions():
    observation = observed(12, time=20)
    original = deepcopy(observation)
    plan = reference_plan(REFERENCE_PROFILE, observation)
    evaluation = evaluate_mission("full_match", observation)
    task = get_role_task("tactics", evaluation, observation, reference_plan=plan)
    focus = task["reference_plan"]["focus"]
    assert "currently visible living enemy structures at their own observed coordinates" in focus
    assert "enemy_start is an initial search hint" in focus
    assert "Empty vision does not prove the map clear" in focus
    assert "bring the main army toward the known initial hint" in focus
    assert "One scout is not the main army" in focus
    assert "continue MAIN-ARMY search of other known resource" not in focus
    assert task["reference_plan"]["continuous_duty"]["activity"] == "advance_main_army_and_engage_current_targets"
    assert task["reference_plan"]["phase"] == "recommended_assault_phase"
    assert task["role_done"] is False
    assert [gap["metric"] for gap in task["goal_gaps"]] == ["native_victory"]
    assert not any(key in task["reference_plan"] for key in ("actions", "target", "destination", "success"))
    assert observation == original


def test_supply_blocker_focus_is_role_scoped_and_still_does_not_propose_an_action():
    observation = observed(12)
    observation["action_state"] = {"train_marine": {"available": False, "reason": "insufficient_supply"}}
    plan = reference_plan(REFERENCE_PROFILE, observation)
    evaluation = evaluate_mission("full_match", observation)
    economy = get_role_task("economy", evaluation, observation, reference_plan=plan)["reference_plan"]
    production = get_role_task("production", evaluation, observation, reference_plan=plan)["reference_plan"]
    assert economy["focus"].startswith("Current native Marine production reports insufficient_supply")
    assert not production["focus"].startswith("Current native Marine production")
    assert "A second base is optional" in economy["focus"]
    assert "actions" not in economy and "actions" not in production


class SyntheticSequenceBackend:
    """Protocol fixture; engine labels here are synthetic, not native evidence."""

    def __init__(self, states):
        self.states, self.index = deepcopy(states), 0
        self.commands = []

    async def start(self, _config, _run_dir):
        return {"observation": self.states[0], "receipts": []}

    async def step(self, commands, _advance):
        self.commands.extend(commands)
        self.index += 1
        state = self.states[self.index]
        return {"observation": state, "receipts": [], "terminal": state["result"] is not None}

    async def close(self):
        return {"status": "closed"}


class ObservingPolicy:
    def __init__(self):
        self.calls = []

    async def decide(self, role, observation, _messages):
        self.calls.append((role, deepcopy(observation)))
        return {"goal": "Observe only", "reason": "No command selected in fixture", "actions": [], "messages": [],
                "request_count": 0, "usage": {"input_tokens": 0, "output_tokens": 0}}


async def run_fixture(tmp_path, states, *, profile=REFERENCE_PROFILE, engine="sc2"):
    backend, policy = SyntheticSequenceBackend(states), ObservingPolicy()
    manager = RunManager(Settings(tmp_path), backend_factory=lambda _config: backend,
                         policy_factory=lambda _config: policy)
    config = RunConfig(backend=engine, policy="llm", scenario="full_match", max_rounds=len(states) - 1,
                       guidance_profile=profile)
    created = await manager.create(config)
    await asyncio.wait_for(manager.tasks[created["id"]], 10)
    return manager, manager.get(created["id"]), backend, policy


async def test_all_three_roles_remain_scheduled_and_advisory_phase_is_persisted_without_commands(tmp_path):
    manager, run, backend, policy = await run_fixture(tmp_path, [
        observed(12), observed(3, time=10), observed(5, time=20), observed(1, time=30, result="Victory"),
    ])
    assert run["result"] == "success" and run["config"]["guidance_profile"] == REFERENCE_PROFILE
    assert len(policy.calls) == 9 and [role for role, _obs in policy.calls] == list(ROLES) * 3
    assert all(not obs["task"]["role_done"] for _role, obs in policy.calls)
    assert all(obs["task"]["reference_plan"]["phase"] == "recommended_assault_phase" for _role, obs in policy.calls)
    assert backend.commands == [] and run["metrics"]["commands_proposed"] == 0
    events = list(manager.store.all_evidence("events", run["id"]))
    phase_events = [event for event in events if event["type"] == "coordination.reference_phase_changed"]
    assert len(phase_events) == 1 and phase_events[0]["actor_id"] == "system"
    assert phase_events[0]["data"]["not_an_agent_action"] is True
    assert phase_events[0]["data"]["revision"] == 3
    assert not any(event["type"] == "coordination.role_wait" for event in events)
    snapshots = list(manager.store.all_evidence("snapshots", run["id"]))
    assert all(snapshot["reference_plan"]["phase"] == "recommended_assault_phase" for snapshot in snapshots)
    assert all({target["role"] for target in snapshot["reference_plan"]["soft_targets"]}
               == {"economy", "production"} for snapshot in snapshots)
    assert all("reference_plan" not in snapshot["observation"] for snapshot in snapshots)
    assert run["reference_plan"]["reference_size_reached_at_game_time"] == 0
    assert {target["role"] for target in run["reference_plan"]["soft_targets"]} == {"economy", "production"}


async def test_reference_phase_transitions_preserve_role_scheduling_and_only_record_advisory_events(tmp_path):
    opening = observed()
    opening.update(workers=12, ready_counts={"SCV": 12}, structures={"COMMANDCENTER": 1})
    manager, run, backend, policy = await run_fixture(tmp_path, [
        opening, observed(time=10), observed(12, time=20), observed(1, time=30),
        observed(1, time=40, result="Victory"),
    ])
    assert run["result"] == "success" and len(policy.calls) == 12
    assert [obs["task"]["reference_plan"]["phase"] for role, obs in policy.calls if role == "tactics"] == [
        "reference_opening", "form_reference_force", "recommended_assault_phase", "recommended_assault_phase"]
    events = list(manager.store.all_evidence("events", run["id"]))
    phase_events = [event for event in events if event["type"] == "coordination.reference_phase_changed"]
    assert [event["data"]["to_phase"] for event in phase_events] == [
        "reference_opening", "form_reference_force", "recommended_assault_phase"]
    assert all(event["data"]["revision"] == 3 and event["data"]["not_an_agent_action"] for event in phase_events)
    assert backend.commands == [] and all(not obs["task"]["role_done"] for _, obs in policy.calls)
    assert all(snapshot["reference_plan"]["revision"] == 3
               for snapshot in manager.store.all_evidence("snapshots", run["id"]))


async def test_native_victory_before_reference_size_succeeds_without_extra_setup_gates(tmp_path):
    initial = observed(0)
    initial.update(workers=0, ready_counts={}, structures={})
    early = deepcopy(initial)
    early.update(game_time=10, game_loop=224, result="Victory")
    _manager, run, backend, policy = await run_fixture(tmp_path, [initial, early])
    assert run["result"] == "success" and run["observation"]["result"] == "Victory"
    assert run["reference_plan"]["phase"] == "reference_opening"
    assert len(policy.calls) == 3 and backend.commands == []
    assert run["evaluation"]["complete"] is True
    assert [milestone["metric"] for milestone in run["milestones"]] == ["native_victory"]


async def test_none_profile_does_not_record_reference_plan_or_phase_events(tmp_path):
    manager, run, _backend, policy = await run_fixture(tmp_path, [observed(0), observed(0, time=10)], profile="none")
    assert "reference_plan" not in run
    assert all("reference_plan" not in obs["task"] for _role, obs in policy.calls)
    assert not any(event["type"] == "coordination.reference_phase_changed"
                   for event in manager.store.all_evidence("events", run["id"]))


async def test_reference_profile_does_not_turn_a_sandbox_terminal_into_native_success(tmp_path):
    _manager, run, _backend, _policy = await run_fixture(tmp_path, [
        observed(12, engine="sandbox"), observed(12, time=10, result="Victory", engine="sandbox"),
    ], engine="sandbox")
    assert run["result"] == "incomplete" and run["evaluation"]["outcome"] == "incomplete"

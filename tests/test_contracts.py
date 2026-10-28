import pytest
from pydantic import ValidationError

from sc2_harness.evaluation import milestones, outcome
from sc2_harness.models import RunConfig
from sc2_harness.tools import arbitrate


def test_role_authority_and_shared_resource_budget():
    accepted, rejected = arbitrate({
        "economy": {"actions": [{"tool": "build_supply", "args": {}}]},
        "production": {"actions": [{"tool": "build_barracks", "args": {}}]},
        "tactics": {"actions": [{"tool": "train_worker", "args": {}}]},
    }, {"minerals": 200, "vespene": 0})
    assert [c["tool"] for c in accepted] == ["build_supply"]
    assert {r["reason"] for r in rejected} == {"shared_budget_exhausted", "role_not_authorized"}


@pytest.mark.parametrize("args", [{"x": float("nan"), "y": 2}, {"x": 2}, {"x": -1, "y": 4},
                                 {"x": 2, "y": 1000}, {"code": "anything"}, {"unit_tag": 5}])
def test_invalid_or_foreign_unit_commands_rejected(args):
    accepted, rejected = arbitrate({"tactics": {"actions": [{"tool": "scout", "args": args}]}},
                                   {"minerals": 0, "vespene": 0, "map": {"width": 64, "height": 64},
                                    "units": [{"tag": "5", "owner": "enemy"}]})
    assert not accepted and len(rejected) == 1


def test_explicit_priority_rotates_shared_budget_without_bypassing_feasibility():
    decisions = {"economy": {"actions": [{"tool": "train_worker", "args": {}}]},
                 "production": {"actions": [{"tool": "build_barracks", "args": {}}]}}
    obs = {"minerals": 150, "vespene": 0}
    commands, _ = arbitrate(decisions, obs, priority=("production", "tactics", "economy"))
    assert [command["tool"] for command in commands] == ["build_barracks"]
    obs["action_state"] = {"build_barracks": {"available": False, "reason": "requires_ready_depot"}}
    commands, rejected = arbitrate(decisions, obs, priority=("production", "tactics", "economy"))
    assert [command["tool"] for command in commands] == ["train_worker"]
    assert rejected[0]["reason"] == "currently_unavailable:requires_ready_depot"
    with pytest.raises(ValueError):
        arbitrate(decisions, obs, priority=("production", "production", "economy"))


def test_engine_acceptance_and_partial_progress_are_not_mission_success():
    obs = {"workers": 14, "structures": {"BARRACKS": 1, "SUPPLYDEPOT": 1},
           "unit_counts": {"MARINE": 0}, "scouted": False, "result": None}
    progress = milestones(obs)
    assert outcome("build_and_scout", obs, progress) is None
    obs.update(unit_counts={"MARINE": 4}, scouted=True)
    assert outcome("build_and_scout", obs, milestones(obs, progress)) == "success"
    assert outcome("full_match", obs, milestones(obs)) is None
    obs["result"] = "Victory"
    assert outcome("full_match", obs, milestones(obs)) == "success"


@pytest.mark.parametrize("changes", [{"map_name": "../../private"}, {"max_rounds": 999999},
                                    {"backend": "auto"}, {"decision_timeout": 0}, {"unknown": "value"}])
def test_unbounded_or_ambiguous_config_rejected(changes):
    with pytest.raises(ValidationError):
        RunConfig(**changes)


@pytest.mark.parametrize("budget", [0, 1, 512, 1024])
def test_reasoning_budget_is_explicit_bounded_and_survives_config_round_trip(budget):
    config = RunConfig(policy="llm", reasoning_budget_tokens=budget)
    assert config.model_dump()["reasoning_budget_tokens"] == budget
    assert RunConfig.model_validate_json(config.model_dump_json()).reasoning_budget_tokens == budget
    assert RunConfig().reasoning_budget_tokens == 0
    assert RunConfig.model_validate({"policy": "llm"}).reasoning_budget_tokens == 0


@pytest.mark.parametrize("budget", [-1, 1025, True, None, "512", 512.0, float("inf")])
def test_reasoning_budget_rejects_unbounded_or_ambiguous_config(budget):
    with pytest.raises(ValidationError):
        RunConfig(reasoning_budget_tokens=budget)

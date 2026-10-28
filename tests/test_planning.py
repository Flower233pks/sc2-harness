from copy import deepcopy

import pytest

from sc2_harness.planning import ROLE_TOOLS, action_context, combat_context


def test_context_filters_actual_role_availability_without_selecting_actions():
    observation = {
        "action_state": {
            "train_marine": {"available": True, "unit_tags": [3], "reason": ""},
            "build_barracks": {"available": False, "reason": "Supply depot unfinished"},
            "attack": {"available": True, "unit_tags": [9]},
        },
        "pending_counts": {"SUPPLYDEPOT": 1},
        "queued_counts": {"MARINE": 2},
        "workers": 14,
        "worker_state": {"idle_tags": [], "harvesting_tags": [1, 2], "eligible_builder_count": 2},
    }
    before = deepcopy(observation)
    context = action_context("production", observation)
    assert context["allowed_tools"] == ["train_marine"]
    assert context["eligible_unit_tags"] == {"train_marine": [3]}
    assert context["blocked_tools"]["build_barracks"] == "Supply depot unfinished"
    assert context["in_progress"] == {"structures": {"SUPPLYDEPOT": 1}, "units": {"MARINE": 2}}
    assert context["worker_saturation"]["eligible_builders"] == 2
    assert context["worker_saturation"]["constructing"] is None
    assert "actions" not in context and "recommended_action" not in context
    assert observation == before


def test_team_blockers_share_only_explicit_current_native_unavailability_without_inferring_prerequisites():
    observation = {
        "ready_counts": {"COMMANDCENTER": 1},
        "action_state": {
            "build_supply": {"available": True},
            "build_barracks": {"available": False, "reason": "no_eligible_unit_or_native_ability"},
            "train_marine": {"available": None, "reason": "unknown"},
            "attack": {"available": False, "reason": "no_owned_army"},
            "secret_tool": {"available": False, "reason": "private debug"},
        },
    }
    context = action_context("economy", observation)
    assert context["allowed_tools"] == ["build_supply"]
    assert context["team_blockers"] == {
        "production": {"build_barracks": "no_eligible_unit_or_native_ability"},
        "tactics": {"attack": "no_owned_army"},
    }
    assert "Supply Depot" not in str(context["team_blockers"])
    assert "train_marine" not in context["team_blockers"]["production"]
    assert action_context("economy", {**observation, "guidance_enabled": False})["team_blockers"] == {}
    assert action_context("economy", {})["team_blockers"] == {}


@pytest.mark.parametrize("state", [None, {}, {"attack": {"available": None}}, {"attack": True}])
def test_missing_current_native_availability_does_not_become_an_allowed_tool(state):
    context = action_context("tactics", {"action_state": state})
    assert context["allowed_tools"] == []
    assert context["availability_known"] is False
    assert set(context["blocked_tools"]) == set(ROLE_TOOLS["tactics"])


def test_old_adapters_keep_static_tools_but_are_explicitly_unchecked():
    context = action_context("economy", {})
    assert context["allowed_tools"] == list(ROLE_TOOLS["economy"])
    assert context["source"] == "legacy-static-unchecked"
    assert context["availability_known"] is False
    context = action_context("tactics", {"guidance_enabled": False, "action_state": {}})
    assert context["allowed_tools"] == list(ROLE_TOOLS["tactics"])
    assert context["source"] == "legacy-guidance-disabled"


def test_receipt_corrections_are_recent_role_filtered_facts():
    receipts = [
        {"actor_id": "production", "tool": "train_marine", "status": "rejected", "reason": "Busy barracks"},
        {
            "actor_id": "economy",
            "tool": "build_supply",
            "status": "rejected",
            "reason": "Insufficient minerals",
        },
        {"actor_id": "production", "tool": "build_barracks", "status": "accepted", "reason": "Accepted"},
    ]
    context = action_context("production", {"last_receipts": receipts})
    assert context["recent_rejections"] == [
        {"tool": "train_marine", "reason": "Busy barracks", "command_id": ""}
    ]


def test_milestone_completion_is_not_a_hidden_action_mask():
    base = {"action_state": {"build_supply": {"available": True, "unit_tags": [1]}}}
    done = {**base, "task": {"scenario": "build_and_scout", "role_done": True}}
    assert action_context("economy", done)["allowed_tools"] == ["build_supply"]
    assert action_context("economy", done) == action_context("economy", base)


@pytest.mark.parametrize("resources", [["gas"], ["minerals"], ["minerals", "gas"], []])
def test_gather_resource_eligibility_is_projected_without_inventing_an_option(resources):
    context = action_context(
        "economy", {"action_state": {"gather": {"available": True, "resources": resources, "unit_tags": [1]}}}
    )
    assert context["gather_resources"] == resources
    assert ("gather" in context["allowed_tools"]) is bool(resources)


def test_scout_identity_projection_preserves_default_and_fails_closed_without_explicit_candidates():
    observation = {
        "action_state": {
            "scout": {
                "available": True,
                "unit_tags": list(range(1, 61)),
                "default_unit_tag": 60,
                "default_available": True,
            }
        }
    }
    context = action_context("tactics", observation)
    assert 60 in context["scout_state"]["unit_tags"]
    assert len(context["scout_state"]["unit_tags"]) == 48
    assert context["scout_state"]["unit_tags"] == context["eligible_unit_tags"]["scout"]
    unavailable = {
        "action_state": {
            "scout": {"available": True, "unit_tags": [], "default_unit_tag": 5, "default_available": False}
        }
    }
    assert "scout" not in action_context("tactics", unavailable)["allowed_tools"]


def test_provided_context_is_role_scoped_and_does_not_override_native_facts():
    provided = {"allowed_tools": ["train_marine", "attack"], "blocked_tools": {}}
    assert action_context("production", {"action_context": provided})["allowed_tools"] == ["train_marine"]
    assert (
        action_context("production", {"action_context": provided, "action_state": {}})["allowed_tools"] == []
    )


def test_execution_failures_preserve_ack_and_only_match_current_command_errors():
    receipt = {
        "command_id": "cmd-1",
        "actor_id": "production",
        "tool": "build_engineering_bay",
        "status": "accepted",
        "details": {
            "unit_tags": [7],
            "ability_id": 322,
            "target": [10, 20],
            "observed_action_errors": [
                {"unit_tag": 7, "ability_id": 322, "result": 208},
                {"unit_tag": 7, "ability_id": 999, "result": 208},
            ],
        },
    }
    observation = {"action_state": {"build_engineering_bay": {"available": True}}, "last_receipts": [receipt]}
    before = deepcopy(observation)
    context = action_context("production", observation)
    failure = context["recent_execution_failures"][0]
    assert failure["ack_status"] == "accepted" and failure["execution_status"] == "observed_native_error"
    assert failure["native_errors"] == [{"unit_tag": 7, "ability_id": 322, "result": 208}]
    assert failure["target"] == [10, 20]
    assert context["recent_rejections"] == []
    assert context["allowed_tools"] == ["build_engineering_bay"]
    assert action_context("economy", observation)["recent_execution_failures"] == []
    assert observation == before


def test_explicit_partial_execution_and_nested_order_errors_are_bounded_facts():
    receipts = [
        {
            "actor_id": "economy",
            "tool": "gather",
            "status": "accepted",
            "details": {"execution_status": "partial", "native_errors": []},
        },
        {
            "actor_id": "economy",
            "tool": "gather",
            "status": "accepted",
            "details": {
                "orders": [{"unit_tags": [9], "ability_id": 295}],
                "native_errors": [
                    {"unit_tag": 9, "ability_id": 295, "result": 208, "result_name": "CouldntReachTarget"}
                ],
            },
        },
        {
            "actor_id": "economy",
            "tool": "gather",
            "status": "accepted",
            "details": {
                "unit_tags": [9],
                "ability_id": 295,
                "native_errors": [{"unit_tag": 10, "ability_id": 295, "result": 208}],
            },
        },
    ]
    failures = action_context("economy", {"last_receipts": receipts})["recent_execution_failures"]
    assert len(failures) == 2
    assert failures[0]["execution_status"] == "partial" and failures[0]["ack_status"] == "accepted"
    assert failures[1]["native_errors"][0]["result_name"] == "CouldntReachTarget"


def test_combat_context_uses_only_current_visible_units_and_finite_reported_cumulative_score():
    import json

    observation = {
        "army_count": 2,
        "ready_counts": {"MARINE": 2},
        "queued_counts": {"MARINE": 1},
        "score": {
            "source": "sc2_native_score",
            "lost_minerals": {"army": 1250},
            "lost_vespene": {"army": float("nan")},
            "killed_minerals": {"army": 350},
        },
        "units": [
            {
                "tag": 1,
                "owner": "self",
                "type": "MARINE",
                "is_ready": True,
                "is_structure": False,
                "health": 9,
                "health_max": 45,
            },
            {
                "tag": 2,
                "owner": "self",
                "type": "MARINE",
                "is_ready": True,
                "is_structure": False,
                "health": 45,
                "health_max": 45,
            },
            {"tag": 3, "owner": "enemy", "type": "ZERGLING", "visible": True},
            {"tag": 4, "owner": "enemy", "type": "HIDDEN_SECRET", "visible": False},
            {"tag": 5, "owner": "neutral", "type": "MINERALFIELD", "visible": True},
        ],
    }
    before = deepcopy(observation)
    out = combat_context(observation)
    assert out["visible_enemy_counts"] == {"ZERGLING": 1}
    assert out["most_damaged_army"] == [
        {"type": "MARINE", "health": 9, "health_max": 45, "health_ratio": 0.2, "tag": 1}
    ]
    assert out["army_score_totals"] == {
        "lost_minerals": 1250,
        "lost_vespene": None,
        "killed_minerals": 350,
        "killed_vespene": None,
    }
    assert out["ready_counts"] == {"MARINE": 2} and out["queued_counts"] == {"MARINE": 1}
    assert "HIDDEN_SECRET" not in json.dumps(out, allow_nan=False)
    assert observation.keys() == before.keys() and observation["units"] == before["units"]
    unknown = combat_context({})
    assert unknown["army_ready"] is None and unknown["ready_counts"] is None
    assert unknown["visible_enemy_counts"] is None and unknown["queued_counts"] is None
    assert all(value is None for value in unknown["army_score_totals"].values())


def test_army_positioning_and_orders_describe_spread_not_fabricated_assembled_force():
    marine = {"owner": "self", "type": "MARINE", "is_structure": False, "is_ready": True}
    observation = {"units": [
        {**marine, "x": 0, "y": 0, "orders": [{"ability": "ATTACK_ATTACK"}]},
        {**marine, "x": 3, "y": 4, "orders": [{"ability": "HOLDPOSITION_HOLD"}]},
        {**marine, "x": 0, "y": 4, "orders": []},
        {**marine, "owner": "enemy", "x": 100, "y": 100},
        {**marine, "type": "SCV", "x": 100, "y": 100},
        {**marine, "is_ready": False, "x": 100, "y": 100},
    ]}
    out = combat_context(observation)
    assert out["army_positioning"] == {
        "ready_units": 3, "positioned_units": 3, "positions_complete": True,
        "max_pair_distance": 5.0, "distance_kind": "euclidean_map_units_not_path_or_combat_range",
    }
    assert out["army_orders"] == {"primary_order_counts": {
        "ATTACK_ATTACK": 1, "HOLDPOSITION_HOLD": 1, "NO_CURRENT_ORDER": 1,
    }, "unknown_order_units": 0}
    assert "assembled" not in out and "recommended_action" not in out


def test_army_position_or_order_unknown_does_not_become_zero_distance_or_idle():
    marine = {"owner": "self", "type": "MARINE", "is_structure": False, "is_ready": True}
    out = combat_context({"units": [{**marine, "x": 0, "y": 0, "orders": []},
                                    {**marine, "x": float("nan"), "y": 5}]})
    assert out["army_positioning"]["max_pair_distance"] is None
    assert out["army_positioning"]["positions_complete"] is False
    assert out["army_orders"] == {"primary_order_counts": {"NO_CURRENT_ORDER": 1},
                                  "unknown_order_units": 1}
    assert combat_context({})["army_orders"] is None
    assert combat_context({"units": []})["army_positioning"]["max_pair_distance"] is None


def test_native_visible_only_payload_projects_targets_and_all_army_health_without_mutation():
    # Native public units omit a visible flag because the adapter already excludes
    # raw fog snapshots. This mirrors the actual native observation contract.
    marine = {"owner": "self", "type": "MARINE", "is_structure": False, "is_ready": True,
              "health": 45.0, "health_max": 45.0, "shield": 0.0, "orders": []}
    observation = {"engine": "sc2", "army_count": 15, "units": [
        *[{**marine, "tag": tag, "x": 10 + tag, "y": 20} for tag in range(1, 15)],
        {**marine, "tag": 15, "health": 3.0, "x": 25, "y": 20},
        {"tag": 4001, "type": "COMMANDCENTER", "owner": "enemy", "is_structure": True,
         "is_ready": True, "x": 38.5, "y": 122.5, "health": 1500.0, "health_max": 1500.0, "shield": 0.0},
        {"tag": 4002, "type": "MARINE", "owner": "enemy", "is_structure": False,
         "is_ready": True, "x": 42.5, "y": 118.5, "health": 33.0, "health_max": 45.0, "shield": 0.0},
    ]}
    before = deepcopy(observation)
    out = combat_context(observation)
    targets = out["visible_enemy_targets"]
    assert targets["structures"] == [{"tag": 4001, "type": "COMMANDCENTER", "x": 38.5, "y": 122.5,
                                       "health": 1500.0, "health_max": 1500.0, "shield": 0.0}]
    assert targets["units"][0]["tag"] == 4002
    assert targets["counts"] == {"structures": 1, "units": 1, "unclassified": 0}
    assert targets["visibility_unknown"] == 0 and targets["visibility_complete"] is True
    assert out["army_health"] == {
        "ready_units": 15, "known_units": 15, "unknown_units": 0, "full_health_units": 14,
        "damaged_units": 1, "health_sum": 633.0, "health_max_sum": 675.0,
        "coverage": 1.0, "coverage_complete": True,
        "scope": "all_ready_owned_nonworker_nonstructures_in_supplied_units_health_only",
    }
    assert len(out["most_damaged_army"]) == 1
    assert observation == before
    targets["structures"][0]["health"] = 0
    assert observation == before  # No references to mutable source target dictionaries escape.


def test_enemy_targets_are_stably_bounded_per_category_with_total_and_omitted_counts():
    enemies = [{"tag": tag, "owner": "enemy", "visible": True, "type": "BARRACKS" if structure else "MARINE",
                "is_structure": structure, "x": tag, "y": 30, "health": 45, "health_max": 45, "shield": 0}
               for structure in (True, False) for tag in range(1 if structure else 101, 10 if structure else 110)]
    forward = combat_context({"units": enemies})["visible_enemy_targets"]
    backward = combat_context({"units": list(reversed(enemies))})["visible_enemy_targets"]
    assert forward == backward
    assert [value["tag"] for value in forward["structures"]] == list(range(1, 9))
    assert [value["tag"] for value in forward["units"]] == list(range(101, 109))
    assert forward["counts"] == {"structures": 9, "units": 9, "unclassified": 0}
    assert forward["omitted"] == {"structures": 1, "units": 1, "unclassified": 0}


def test_explicit_hidden_or_snapshot_flags_never_leak_even_in_a_native_public_payload():
    import json

    enemy = {"owner": "enemy", "is_structure": False, "x": 91, "y": 92}
    observation = {"engine": "sc2", "units": [
        {**enemy, "tag": 1, "type": "FALSE_SECRET", "visible": False},
        {**enemy, "tag": 2, "type": "SNAPSHOT_SECRET", "display_type": 2},
        {**enemy, "tag": 3, "type": "HIDDEN_SECRET", "display_type": 3},
        {**enemy, "tag": 4, "type": "CONFLICT_SECRET", "visible": True, "display_type": 2},
        {**enemy, "tag": 5, "type": "UNKNOWN_SECRET", "visible": None},
        {**enemy, "tag": 6, "type": "MARINE", "display_type": 1, "health": float("nan")},
    ]}
    out = combat_context(observation)
    assert "SECRET" not in json.dumps(out, allow_nan=False)
    assert out["visible_enemy_counts"] == {"MARINE": 1}
    targets = out["visible_enemy_targets"]
    assert targets["visibility_unknown"] == 1 and targets["visibility_complete"] is False
    assert targets["units"] == [{"tag": 6, "type": "MARINE", "x": 91, "y": 92,
                                  "health": None, "health_max": None, "shield": None}]


def test_legacy_unknown_visibility_or_classification_is_explicit_without_invented_targets():
    observation = {"units": [
        {"tag": 1, "owner": "enemy", "type": "UNKNOWN_SECRET", "x": 90, "y": 90},
        {"tag": 2, "owner": "enemy", "type": "VISIBLE_UNCLASSIFIED", "visible": True, "x": 20, "y": 20},
    ]}
    out = combat_context(observation)
    assert out["visible_enemy_counts"] == {"VISIBLE_UNCLASSIFIED": 1}
    targets = out["visible_enemy_targets"]
    assert targets["structures"] == targets["units"] == []
    assert targets["counts"] == {"structures": 0, "units": 0, "unclassified": 1}
    assert targets["omitted"]["unclassified"] == 1
    assert targets["visibility_unknown"] == 1
    assert targets["visibility_basis"] == "explicit_visibility_flags_only"


def test_army_health_coverage_does_not_fabricate_missing_health_or_count_workers_pending_and_enemies():
    marine = {"owner": "self", "type": "MARINE", "is_structure": False, "is_ready": True}
    observation = {"units": [
        {**marine, "health": 3, "health_max": 45},
        {**marine, "health": 45, "health_max": 45},
        {**marine, "health": 30, "shield": 80},
        {**marine, "health": float("inf"), "health_max": 45},
        {**marine, "health": 45, "health_max": 0},
        {**marine, "health": True, "health_max": 45},
        {**marine, "health": 99, "health_max": 45},
        {**marine, "type": "SCV", "health": 1, "health_max": 45},
        {**marine, "is_ready": False, "health": 1, "health_max": 45},
        {**marine, "is_structure": True, "health": 1, "health_max": 45},
        {**marine, "owner": "enemy", "visible": True, "health": 1, "health_max": 45},
    ]}
    health = combat_context(observation)["army_health"]
    assert health["ready_units"] == 7 and health["known_units"] == 2 and health["unknown_units"] == 5
    assert health["damaged_units"] == health["full_health_units"] == 1
    assert health["health_sum"] == 48 and health["health_max_sum"] == 90
    assert health["coverage"] == 0.2857 and health["coverage_complete"] is False
    assert "ready_for_attack" not in health and "recommended_action" not in health


def test_missing_empty_and_unknown_army_payloads_keep_distinct_health_and_visibility_meanings():
    absent = combat_context({})
    assert absent["army_health"] is None and absent["visible_enemy_targets"] is None
    empty = combat_context({"units": []})
    assert empty["army_health"]["ready_units"] == empty["army_health"]["health_sum"] == 0
    assert empty["army_health"]["coverage"] is None
    assert empty["visible_enemy_targets"]["visibility_unknown"] == 0
    unknown = combat_context({"units": [
        {"owner": "self", "type": "MARINE", "is_structure": False, "is_ready": True},
    ]})["army_health"]
    assert unknown["known_units"] == 0 and unknown["unknown_units"] == 1
    assert unknown["health_sum"] is None and unknown["health_max_sum"] is None
    assert unknown["coverage"] == 0.0 and unknown["coverage_complete"] is False

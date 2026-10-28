import hashlib
import json
from copy import deepcopy

import pytest

from scripts.summarize_demo import summarize_demo, write_summary

FINGERPRINT = "a" * 64
MODEL = "Qwen/Qwen3-8B"
MAPS = ("Simple64", "Simple96", "AbyssalReefLE")


def run_data(index=1, map_name="Simple64", **changes):
    config = {"name": "fixture", "backend": "sc2", "policy": "llm", "scenario": "full_match", "seed": 7,
              "map_name": map_name, "max_rounds": 500, "max_game_seconds": 3600, "advance_loops": 224,
              "decision_timeout": 30, "communication": True, "execution_guidance": True, "replay": True}
    metrics = {"wall_seconds": 310, "game_seconds": 700, "engine_result": "Victory", "model_calls": 6,
               "input_tokens": 60, "output_tokens": 6, "latency_p50_ms": 1000, "latency_p95_ms": 1200,
               "commands_proposed": 3, "commands_accepted": 3, "commands_rejected": 0}
    agents = [{"id": role, "stats": {"calls": 2, "input_tokens": 20, "output_tokens": 2}}
              for role in ("economy", "production", "tactics")]
    value = {"id": f"{index:032x}", "status": "completed", "result": "success", "config": config,
             "model_name": MODEL, "source_fingerprint": FINGERPRINT, "evaluation_version": "fixture-version",
             "metrics": metrics, "agents": agents, "observation": {"engine": "sc2", "result": "Victory",
             "game_time": 700, "engine_identity": {"opponent": "builtin_terran_very_easy",
                                                   "map_name": map_name,
                                                   "game_version": "4.10", "base_build": 75689}}}
    value.update(changes)
    return value


def evidence(tmp_path, value, *, events=None, snapshots=None):
    directory = tmp_path / value["id"]
    directory.mkdir()
    for filename, data in [("run.json", value), ("config.json", value["config"]), ("metrics.json", value["metrics"])]:
        (directory / filename).write_text(json.dumps(data), encoding="utf-8")
    if events is None:
        events = []
        for role in ("economy", "production", "tactics"):
            events.extend([
                {"type": "agent.decision", "actor_id": role,
                 "data": {"request_count": 2, "model": MODEL, "policy": "llm"}},
                {"type": "command.proposed", "actor_id": role, "data": {"tool": "scout"}},
                {"type": "command.accepted", "actor_id": role, "data": {"tool": "scout"}},
                {"type": "agent.message", "actor_id": role, "data": {"text": "fixture"}},
            ])
    if snapshots is None:
        snapshots = [{"observation": {"ready_counts": {"MARINE": count}, "units": [
            *[{"type": "MARINE", "owner": "self", "is_ready": True, "is_structure": False} for _ in range(count)],
            {"type": "MARINE", "owner": "self", "is_ready": False, "is_structure": False},
            {"type": "SCV", "owner": "self", "is_ready": True, "is_structure": False},
            {"type": "BARRACKS", "owner": "self", "is_ready": True, "is_structure": True},
        ]}} for count in (2, 5, 0)]
    for filename, rows in [("events.jsonl", events), ("snapshots.jsonl", snapshots)]:
        (directory / filename).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return directory


def rewrite(directory, value):
    for name, content in [("run.json", value), ("config.json", value["config"]), ("metrics.json", value["metrics"])]:
        (directory / name).write_text(json.dumps(content), encoding="utf-8")


def test_three_maps_same_frozen_configuration_with_native_wins_pass(tmp_path):
    directories = [evidence(tmp_path, run_data(index, name)) for index, name in enumerate(MAPS, 1)]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for folder in directories for path in folder.iterdir()}
    summary = summarize_demo(directories, fingerprint=FINGERPRINT, model_name=MODEL)
    assert summary["demo_pass"] is True and len(summary["cohorts"]) == 1
    assert summary["rates"]["attempt_records"] == summary["rates"]["qualifying_demo_wins"] == 3
    assert all(summary["cohorts"][0]["maps"][name]["qualifying_run_ids"] for name in MAPS)
    assert summary["cohorts"][0]["identity"]["config"]["reasoning_budget_tokens"] is None
    assert summary["cohorts"][0]["identity"]["inference_settings"] is None
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                      for folder in directories for path in folder.iterdir()}


def test_three_seeds_on_one_map_are_not_three_maps(tmp_path):
    directories = []
    for index, seed in enumerate((7, 19, 41), 1):
        value = run_data(index)
        value["config"]["seed"] = seed
        directories.append(evidence(tmp_path, value))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is False
    assert summary["rates"]["confirmed_native_victories"] == 3
    assert all(not group["maps"]["Simple96"]["qualifying_run_ids"] for group in summary["cohorts"])


@pytest.mark.parametrize("change", ["fingerprint", "model", "budget", "seed", "guidance", "opponent"])
def test_different_versions_models_configs_or_opponents_cannot_be_mixed_to_pass(tmp_path, change):
    values = [run_data(index, name) for index, name in enumerate(MAPS, 1)]
    changed = values[-1]
    if change == "fingerprint":
        changed["source_fingerprint"] = "b" * 64
    elif change == "model":
        changed["model_name"] = "Qwen/Qwen3-4B"
    elif change == "budget":
        changed["config"]["max_rounds"] = 400
    elif change == "seed":
        changed["config"]["seed"] = 19
    elif change == "guidance":
        changed["config"]["execution_guidance"] = False
    else:
        changed["observation"]["engine_identity"]["opponent"] = "builtin_terran_easy"
    directories = [evidence(tmp_path, value) for value in values]
    assert summarize_demo(directories)["demo_pass"] is False


@pytest.mark.parametrize("case", ["wall_short", "wall_unknown", "game_only", "defeat", "specialized", "scripted",
                                 "sandbox", "inactive_role", "unknown_role_calls", "unknown_model", "not_completed"])
def test_non_qualifying_or_unknown_conditions_are_not_demo_wins(tmp_path, case):
    value = run_data()
    if case == "wall_short": value["metrics"]["wall_seconds"] = 299.99
    elif case == "wall_unknown": value["metrics"]["wall_seconds"] = None
    elif case == "game_only":
        value["metrics"]["wall_seconds"] = 20
        value["observation"]["game_time"] = 1000
    elif case == "defeat":
        value["observation"]["result"] = value["metrics"]["engine_result"] = "Defeat"
        value["result"] = "failure"
    elif case == "specialized": value["config"]["scenario"] = "army_readiness"
    elif case == "scripted": value["config"]["policy"] = "scripted"
    elif case == "sandbox": value["observation"]["engine"] = value["config"]["backend"] = "sandbox"
    elif case == "inactive_role":
        value["agents"][2]["stats"]["calls"] = 0
        value["metrics"]["model_calls"] = 4
    elif case == "unknown_role_calls": value["agents"] = []
    elif case == "unknown_model": value["model_name"] = None
    else: value["status"] = "running"
    events = [] if case in {"unknown_model", "unknown_role_calls"} else None
    attempt = summarize_demo([evidence(tmp_path, value, events=events)])["attempts"][0]
    assert attempt["qualifying_win"] is not True
    if case == "wall_unknown": assert "wall_at_least_300s" in attempt["unknown_checks"]
    if case == "unknown_role_calls": assert attempt["roles"]["tactics"]["model_calls"] is None


def test_all_supplied_attempts_remain_in_rate_denominator_including_unknowns_and_errors(tmp_path):
    victory = evidence(tmp_path, run_data(1))
    defeat_data = run_data(2, result="failure")
    defeat_data["observation"]["result"] = defeat_data["metrics"]["engine_result"] = "Defeat"
    defeat = evidence(tmp_path, defeat_data)
    report = tmp_path / "partial.json"
    report.write_text(json.dumps({"complete": False, "planned_sample_count": 5,
        "runs": [{"id": "3" * 32, "status": "failed", "result": "error", "metrics": {}},
                 {"id": None, "status": "unconfirmed", "result": None}]}), encoding="utf-8")
    summary = summarize_demo([victory, defeat], [report])
    assert len(summary["attempts"]) == 4
    assert summary["rates"]["confirmed_native_victories"] == 1
    assert summary["rates"]["native_win_rate_over_all_supplied_attempts"] == 0.25
    assert summary["rates"]["mission_results"] == {"success": 1, "failure": 1, "error": 1, "unknown": 1}
    assert summary["report_metadata"][0]["complete"] is False
    assert summary["report_metadata"][0]["planned_sample_count"] == 5  # No invented unstarted attempts.


def test_duplicate_report_and_evidence_are_one_attempt_but_conflicting_results_block_qualification(tmp_path):
    value = run_data()
    directory = evidence(tmp_path, value)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"runs": [value]}), encoding="utf-8")
    summary = summarize_demo([directory], [report])
    assert len(summary["attempts"]) == 1 and summary["attempts"][0]["qualifying_win"] is True
    value["result"] = "failure"
    report.write_text(json.dumps({"runs": [value]}), encoding="utf-8")
    summary = summarize_demo([directory], [report])
    assert len(summary["attempts"]) == 1 and summary["attempts"][0]["qualifying_win"] is False
    assert "duplicate_result_disagrees" in summary["attempts"][0]["conflicts"]


def test_role_event_counts_and_ready_army_peak_do_not_count_queue_or_pending_units(tmp_path):
    value = run_data()
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["peaks"]["ready_army"] == attempt["peaks"]["ready_marines"] == 5
    assert attempt["roles"]["economy"]["event_counts"] == {
        "proposed": 1, "accepted": 1, "initial_rejected": 0, "execution_failed": 0,
        "execution_partial": 0, "messages_sent": 1}
    assert attempt["metrics"]["commands_execution_failed"] is None  # Missing is not zero.
    assert attempt["metrics"]["round_decision_latency_p50_ms"] is None


def test_missing_or_corrupt_evidence_stays_unknown_and_cannot_silently_pass(tmp_path):
    directory = evidence(tmp_path, run_data())
    (directory / "events.jsonl").unlink()
    (directory / "snapshots.jsonl").write_text('{"bad":', encoding="utf-8")
    summary = summarize_demo([directory])
    attempt = summary["attempts"][0]
    assert len(summary["input_issues"]) == 2
    assert attempt["roles"]["economy"]["event_counts"]["messages_sent"] is None
    assert attempt["peaks"]["ready_army"] is None and attempt["peaks"]["snapshots"] is None
    assert summary["demo_pass"] is False


def test_tokens_allow_only_complete_aligned_role_ledger_and_do_not_invent_latency(tmp_path):
    value = run_data()
    value["metrics"]["input_tokens"] = value["metrics"]["output_tokens"] = None
    value["agents"][2]["stats"] = {"calls": 0, "input_tokens": None, "output_tokens": None}
    value["metrics"]["model_calls"] = 4
    directory = evidence(tmp_path, value)
    attempt = summarize_demo([directory])["attempts"][0]
    assert attempt["tokens"]["input_tokens"] == 40
    assert attempt["tokens"]["input_tokens_source"] == "derived_role_ledger"
    value["agents"][0]["stats"]["input_tokens"] = None
    rewrite(directory, value)
    attempt = summarize_demo([directory])["attempts"][0]
    assert attempt["tokens"]["input_tokens"] is None
    assert attempt["tokens"]["output_tokens"] == 4
    assert attempt["metrics"]["latency_p50_ms"] == 1000
    assert attempt["metrics"]["round_decision_latency_p95_ms"] is None


def test_inconsistent_native_results_and_call_ledgers_do_not_qualify(tmp_path):
    value = run_data()
    value["metrics"]["engine_result"] = "Defeat"
    value["metrics"]["model_calls"] = 100
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert set(attempt["conflicts"]) == {"native_terminal_disagrees", "global_role_calls_disagree"}


def test_missing_opponent_is_unknown_and_call_conflict_does_not_rewrite_observed_victory(tmp_path):
    value = run_data()
    del value["observation"]["engine_identity"]["opponent"]
    value["metrics"]["model_calls"] = 99
    summary = summarize_demo([evidence(tmp_path, value)])
    attempt = summary["attempts"][0]
    assert attempt["checks"]["computer_opponent"] is None
    assert attempt["comparison_warnings"] == ["opponent_not_recorded"]
    assert attempt["qualifying_win"] is False
    assert summary["rates"]["confirmed_native_victories"] == 1


def test_unknown_ready_unit_metadata_does_not_invent_zero_army(tmp_path):
    snapshots = [{"observation": {"units": [{"type": "MARINE"}]}}]
    attempt = summarize_demo([evidence(tmp_path, run_data(), snapshots=snapshots)])["attempts"][0]
    assert attempt["peaks"]["ready_army"] is None


def test_fingerprint_selection_retains_nonselected_attempts(tmp_path):
    directories = [evidence(tmp_path, run_data(index, name)) for index, name in enumerate(MAPS, 1)]
    summary = summarize_demo(directories, fingerprint="b" * 64)
    assert len(summary["attempts"]) == 3 and summary["demo_pass"] is False
    assert summary["cohorts"][0]["selected"] is False


def test_summary_output_cannot_overwrite_evidence_or_reports(tmp_path):
    directory = evidence(tmp_path, run_data())
    summary = summarize_demo([directory])
    before = (directory / "run.json").read_bytes()
    for output in (directory / "run.json", directory / "new-summary.json"):
        with pytest.raises(ValueError):
            write_summary(output, summary, evidence_dirs=[directory])
    output = tmp_path / "summary.json"
    write_summary(output, summary, evidence_dirs=[directory])
    assert json.loads(output.read_text(encoding="utf-8"))["rates"]["attempt_records"] == 1
    with pytest.raises(ValueError):
        write_summary(output, summary)
    assert (directory / "run.json").read_bytes() == before


def test_empty_input_keeps_rate_unknown_not_zero_and_configuration_metadata_missing_is_explicit():
    summary = summarize_demo()
    assert summary["rates"]["native_win_rate_over_all_supplied_attempts"] is None
    assert summary["demo_pass"] is False


def test_native_map_identity_prevents_relabeling_one_map_as_three(tmp_path):
    directories = []
    for index, map_name in enumerate(MAPS, 1):
        value = run_data(index, map_name)
        value["observation"]["engine_identity"]["map_name"] = "Simple64"
        directories.append(evidence(tmp_path, value))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is False
    assert summary["attempts"][0]["qualifying_win"] is True
    for attempt in summary["attempts"][1:]:
        assert attempt["native_map_name"] == "Simple64"
        assert attempt["qualifying_win"] is False
        assert "native_map_disagrees_with_config" in attempt["conflicts"]


def test_missing_native_map_identity_is_unknown_not_a_verified_map(tmp_path):
    value = run_data()
    del value["observation"]["engine_identity"]["map_name"]
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["checks"]["native_map_matches_config"] is None
    assert attempt["qualifying_win"] is None


def test_scripted_zero_request_events_cannot_be_overridden_by_claimed_llm_ledger(tmp_path):
    events = [{"type": "agent.decision", "actor_id": role,
               "data": {"request_count": 0, "policy": "scripted", "model": MODEL}}
              for role in ("economy", "production", "tactics")]
    attempt = summarize_demo([evidence(tmp_path, run_data(), events=events)])["attempts"][0]
    assert attempt["checks"]["actual_llm_call_evidence"] is False
    assert attempt["qualifying_win"] is False
    assert all(f"{role}_non_llm_decision_policy" in attempt["conflicts"]
               for role in ("economy", "production", "tactics"))
    assert attempt["call_evidence"]["economy"]["recorded_event_requests"] == 0


def test_report_only_metadata_calls_are_not_positive_actual_llm_proof(tmp_path):
    report = tmp_path / "metadata-only.json"
    report.write_text(json.dumps({"runs": [run_data()]}), encoding="utf-8")
    attempt = summarize_demo(report_paths=[report])["attempts"][0]
    assert attempt["checks"]["all_three_roles_called"] is True
    assert attempt["checks"]["actual_llm_call_evidence"] is None
    assert attempt["checks"]["role_call_ledgers_consistent"] is None
    assert attempt["qualifying_win"] is None
    assert attempt["call_evidence"]["economy"]["coverage"] == "unknown_request_coverage"


def test_inflight_or_cancelled_requests_keep_ledger_but_leave_coverage_unknown(tmp_path):
    value = run_data()
    for agent in value["agents"]:
        agent["stats"]["calls"] = 3
    value["metrics"]["model_calls"] = 9
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["checks"]["actual_llm_call_evidence"] is True
    assert attempt["checks"]["role_call_ledgers_consistent"] is None
    assert attempt["qualifying_win"] is None
    assert attempt["roles"]["economy"]["model_calls"] == 3
    proof = attempt["call_evidence"]["economy"]
    assert proof["recorded_event_requests"] == 2
    assert "possible_inflight_or_partial_trace" in proof["coverage"]
    assert not attempt["conflicts"]  # Unreconciled coverage is unknown, not a fabricated contradiction.


def test_positive_llm_evidence_need_not_have_policy_metadata_on_every_other_turn(tmp_path):
    events = []
    for role in ("economy", "production", "tactics"):
        events.extend([
            {"type": "agent.decision", "actor_id": role,
             "data": {"request_count": 1, "policy": "llm", "model": MODEL}},
            {"type": "agent.error", "actor_id": role, "data": {"request_count": 1}},
        ])
    attempt = summarize_demo([evidence(tmp_path, run_data(), events=events)])["attempts"][0]
    assert attempt["qualifying_win"] is True
    assert attempt["checks"]["role_call_ledgers_consistent"] is True


def test_duplicate_known_defeat_is_never_dropped_in_favor_of_native_victory(tmp_path):
    value = run_data()
    directory = evidence(tmp_path, value)
    contradictory = deepcopy(value)
    contradictory["metrics"]["engine_result"] = contradictory["observation"]["result"] = "Defeat"
    report = tmp_path / "contradiction.json"
    report.write_text(json.dumps({"runs": [contradictory]}), encoding="utf-8")
    later = tmp_path / "matching.json"
    later.write_text(json.dumps({"runs": [value]}), encoding="utf-8")
    summary = summarize_demo([directory], [report, later])
    attempt = summary["attempts"][0]
    assert len(summary["attempts"]) == 1
    assert "duplicate_native_terminal_disagrees" in attempt["conflicts"]
    assert attempt["native_results_observed"] == ["Defeat", "Victory"]
    assert attempt["engine_result"] is None
    assert attempt["qualifying_win"] is False
    assert summary["rates"]["confirmed_native_victories"] == 0


def inference_settings(budget):
    return {"temperature": 0.7, "enable_thinking": budget > 0,
            "reasoning_format": "deepseek" if budget else None,
            "reasoning_budget_tokens": budget or None,
            "max_output_tokens": 1536 if budget else 768,
            "history_turns": 1 if budget else 3, "strict_schema": True, "context_window_tokens": None}


def decision_events(settings):
    return [{"type": "agent.decision", "actor_id": role,
             "data": {"request_count": 2, "model": MODEL, "policy": "llm", "inference_settings": settings}}
            for role in ("economy", "production", "tactics")]


def test_reasoning_budgets_zero_512_and_1024_are_distinct_cohorts(tmp_path):
    directories = []
    for index, (name, budget) in enumerate(zip(MAPS, (0, 512, 1024), strict=True), 1):
        value = run_data(index, name)
        value["config"]["reasoning_budget_tokens"] = budget
        directories.append(evidence(tmp_path, value))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is False and len(summary["cohorts"]) == 3
    assert {group["identity"]["config"]["reasoning_budget_tokens"] for group in summary["cohorts"]} == {0, 512, 1024}
    assert all(item["qualifying_win"] is True for item in summary["attempts"])


def test_legacy_unknown_budget_is_not_inferred_as_explicit_off(tmp_path):
    values = [run_data(index, name) for index, name in enumerate(MAPS, 1)]
    values[-1]["config"]["reasoning_budget_tokens"] = 0
    summary = summarize_demo([evidence(tmp_path, value) for value in values])
    assert summary["demo_pass"] is False and len(summary["cohorts"]) == 2
    assert {group["identity"]["config"]["reasoning_budget_tokens"] for group in summary["cohorts"]} == {None, 0}
    assert all(item["comparison_complete"] for item in summary["attempts"])


@pytest.mark.parametrize("budget", [0, 512, 1024])
def test_same_explicit_budget_and_recorded_generation_settings_can_pass_three_maps(tmp_path, budget):
    directories = []
    for index, name in enumerate(MAPS, 1):
        value = run_data(index, name)
        value["config"]["reasoning_budget_tokens"] = budget
        directories.append(evidence(tmp_path, value, events=decision_events(inference_settings(budget))))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is True and len(summary["cohorts"]) == 1
    assert summary["cohorts"][0]["identity"]["inference_settings"] == inference_settings(budget)


def test_recorded_generation_settings_separate_cohorts_without_guessing_missing_defaults(tmp_path):
    directories = []
    for index, name in enumerate(MAPS, 1):
        settings = inference_settings(0)
        settings["temperature"] = 0.5 if index == 3 else 0.7
        directories.append(evidence(tmp_path, run_data(index, name), events=decision_events(settings)))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is False and len(summary["cohorts"]) == 2
    assert all(item["qualifying_win"] is True for item in summary["attempts"])


def test_conflicting_settings_within_run_or_duplicate_report_block_qualification(tmp_path):
    value = run_data()
    events = decision_events(inference_settings(0))
    events[-1]["data"]["inference_settings"] = inference_settings(512)
    directory = evidence(tmp_path, value, events=events)
    attempt = summarize_demo([directory])["attempts"][0]
    assert attempt["qualifying_win"] is False and attempt["inference_settings"] is None
    assert "inference_settings_disagree" in attempt["conflicts"]
    assert len(attempt["inference_settings_observed"]) == 2
    # A richer export must not silently discard contradictory historical settings in a report.
    (directory / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in
        decision_events(inference_settings(0))), encoding="utf-8")
    value["inference_settings"] = inference_settings(512)
    report = tmp_path / "settings-conflict.json"
    report.write_text(json.dumps({"runs": [value]}), encoding="utf-8")
    attempt = summarize_demo([directory], [report])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert "inference_settings_disagree" in attempt["conflicts"]


@pytest.mark.parametrize("budget", [True, -1, 1025, "512"])
def test_invalid_explicit_reasoning_budget_does_not_qualify(tmp_path, budget):
    value = run_data()
    value["config"]["reasoning_budget_tokens"] = budget
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert "invalid_reasoning_budget_tokens" in attempt["conflicts"]


def test_config_off_cannot_override_recorded_enabled_thinking(tmp_path):
    value = run_data()
    value["config"]["reasoning_budget_tokens"] = 0
    attempt = summarize_demo([evidence(tmp_path, value,
        events=decision_events(inference_settings(512)))])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert "reasoning_budget_disagrees_with_inference_settings" in attempt["conflicts"]


@pytest.mark.parametrize("contexts,expected", [([8192, 16384, 16384], False),
                                               ([16384, 16384, 16384], True),
                                               ([None, 16384, 16384], False)])
def test_recorded_context_windows_do_not_mix_8k_16k_or_legacy_unknown(tmp_path, contexts, expected):
    directories = []
    for index, (name, context) in enumerate(zip(MAPS, contexts, strict=True), 1):
        settings = inference_settings(512)
        if context is None:
            del settings["context_window_tokens"]  # A legacy event is unknown, never inferred as 8K or 16K.
        else:
            settings["context_window_tokens"] = context
        directories.append(evidence(tmp_path, run_data(index, name), events=decision_events(settings)))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is expected
    assert all(item["qualifying_win"] is True for item in summary["attempts"])
    assert all(item["comparison_complete"] for item in summary["attempts"])
    actual = {group["identity"]["inference_settings"]["context_window_tokens"] for group in summary["cohorts"]}
    assert actual == set(contexts)


def test_context_window_conflict_within_one_run_is_preserved(tmp_path):
    settings = inference_settings(512)
    settings["context_window_tokens"] = 8192
    events = decision_events(settings)
    events[-1]["data"]["inference_settings"] = {**settings, "context_window_tokens": 16384}
    attempt = summarize_demo([evidence(tmp_path, run_data(), events=events)])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert "inference_settings_disagree" in attempt["conflicts"]
    assert {item["context_window_tokens"] for item in attempt["inference_settings_observed"]} == {8192, 16384}


@pytest.mark.parametrize("profile", ["none", "marine_reference_v1"])
def test_same_recorded_guidance_profile_can_qualify_three_native_wins(tmp_path, profile):
    directories = []
    for index, name in enumerate(MAPS, 1):
        value = run_data(index, name)
        value["config"]["guidance_profile"] = profile
        directories.append(evidence(tmp_path, value))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is True and len(summary["cohorts"]) == 1
    assert summary["cohorts"][0]["identity"]["config"]["guidance_profile"] == profile
    assert all(item["qualifying_win"] is True for item in summary["attempts"])


def test_unknown_none_and_reference_guidance_cannot_be_mixed_into_three_maps(tmp_path):
    directories = []
    for index, (name, profile) in enumerate(zip(MAPS, (None, "none", "marine_reference_v1"), strict=True), 1):
        value = run_data(index, name)
        if profile is not None:
            value["config"]["guidance_profile"] = profile
        directories.append(evidence(tmp_path, value))
    summary = summarize_demo(directories)
    assert summary["demo_pass"] is False and len(summary["cohorts"]) == 3
    profiles = {group["identity"]["config"]["guidance_profile"] for group in summary["cohorts"]}
    assert profiles == {None, "none", "marine_reference_v1"}
    assert all(item["comparison_complete"] for item in summary["attempts"])
    assert all(item["qualifying_win"] is True for item in summary["attempts"])


@pytest.mark.parametrize("profile", [True, "marine_reference", "", 3])
def test_invalid_recorded_guidance_profile_does_not_qualify(tmp_path, profile):
    value = run_data()
    value["config"]["guidance_profile"] = profile
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert "invalid_guidance_profile" in attempt["conflicts"]


def test_reference_soft_force_goal_never_replaces_native_victory_criterion(tmp_path):
    victory = run_data(1)
    victory["config"]["guidance_profile"] = "marine_reference_v1"
    victory["observation"]["ready_counts"] = {"MARINE": 3}  # Fewer than the reference's soft twelve-unit goal.
    defeat = run_data(2, result="failure")
    defeat["config"]["guidance_profile"] = "marine_reference_v1"
    defeat["observation"]["ready_counts"] = {"MARINE": 20}
    defeat["observation"]["result"] = defeat["metrics"]["engine_result"] = "Defeat"
    attempts = summarize_demo([evidence(tmp_path, victory), evidence(tmp_path, defeat)])["attempts"]
    assert attempts[0]["qualifying_win"] is True
    assert attempts[1]["qualifying_win"] is False
    assert attempts[1]["checks"]["native_victory"] is False


def test_reference_profile_cannot_claim_guidance_disabled_execution(tmp_path):
    value = run_data()
    value["config"].update(guidance_profile="marine_reference_v1", execution_guidance=False)
    attempt = summarize_demo([evidence(tmp_path, value)])["attempts"][0]
    assert attempt["qualifying_win"] is False
    assert "guidance_profile_config_disagrees" in attempt["conflicts"]

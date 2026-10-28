from sc2_harness.telemetry import record_receipt, update_metrics


def test_partial_and_empty_runs_do_not_acquire_fake_completion_times():
    run = {"metrics": {}, "result": "incomplete", "observation": {"game_time": 300}}
    update_metrics(run)
    assert run["metrics"]["command_acceptance_rate"] is None
    assert run["metrics"]["milestone_completion_rate"] is None
    assert run["metrics"]["task_completion_game_seconds"] is None
    assert run["metrics"]["stage_completion_rate"] is None


def test_denominators_rejections_and_native_outcomes_are_separate():
    run = {"metrics": {"commands_proposed": 4, "commands_accepted": 1, "commands_rejected": 3,
                       "wall_seconds": 90}, "result": "success", "observation": {"game_time": 120,
                       "result": None, "score": {"collected_minerals": 2000}},
           "milestones": [{"achieved": True}, {"achieved": True}],
           "evaluation": {"stages": [{"complete": True}, {"complete": False}]}}
    record_receipt(run["metrics"], {"status": "rejected", "reason": "busy", "actor_id": "production"})
    record_receipt(run["metrics"], {"status": "accepted", "actor_id": "production"})
    update_metrics(run)
    assert run["metrics"]["command_rejection_rate"] == .75
    assert run["metrics"]["command_acceptance_rate"] == .25
    assert run["metrics"]["rejection_reasons"] == {"busy": 1}
    assert run["metrics"]["rejections_by_role"] == {"production": 1}
    assert run["metrics"]["stage_completion_rate"] == .5
    assert run["metrics"]["task_completion_game_seconds"] == 120
    assert run["metrics"]["task_completion_wall_seconds"] == 90
    assert run["metrics"]["native_score"]["collected_minerals"] == 2000
    assert run["observation"]["result"] is None


def test_later_execution_failures_do_not_rewrite_initial_acceptance_or_fake_completion():
    metrics = {"commands_accepted": 2, "commands_rejected": 0, "commands_completed": 0}
    error = {"unit_tag": 7, "ability_id": 321, "result": 208, "result_name": "CouldntReachTarget"}
    for execution in ("failed", "partial"):
        record_receipt(metrics, {"status": "accepted", "actor_id": "production", "details": {
            "engine_results": [1], "execution_status": execution, "native_errors": [error]}})
    record_receipt(metrics, {"status": "accepted", "details": {"execution_status": "pending_observation"}})
    assert metrics["commands_execution_failed"] == 1
    assert metrics["commands_execution_partial"] == 1
    assert metrics["execution_failure_reasons"] == {"CouldntReachTarget": 2}
    assert metrics["commands_accepted"] == 2 and metrics["commands_rejected"] == 0
    assert metrics["commands_completed"] == 0
    assert "rejection_reasons" not in metrics

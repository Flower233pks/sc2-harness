"""Descriptive evaluation metrics, never substitutes for observed mission outcomes."""
from __future__ import annotations

import hashlib
from pathlib import Path


def source_fingerprint() -> str:
    """Identify installed package sources without reading environment or runtime files."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    paths = sorted(p for p in root.rglob("*") if p.suffix in {".py", ".js", ".html", ".css"})
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def record_receipt(metrics: dict, receipt: dict) -> None:
    details = receipt.get("details") or {}
    execution = details.get("execution_status") if isinstance(details, dict) else None
    if receipt.get("status") == "accepted" and execution in {"failed", "partial"}:
        key = "commands_execution_failed" if execution == "failed" else "commands_execution_partial"
        metrics[key] = metrics.get(key, 0) + 1
        reasons = metrics.setdefault("execution_failure_reasons", {})
        errors = details.get("native_errors", [])
        for error in errors if isinstance(errors, list) else []:
            if isinstance(error, dict):
                reason = str(error.get("result_name") or error.get("result") or "unknown_native_error")
                reasons[reason] = reasons.get(reason, 0) + 1
    if receipt.get("status") != "rejected":
        return
    reason = str(receipt.get("reason", "unspecified"))
    reasons = metrics.setdefault("rejection_reasons", {})
    reasons[reason] = reasons.get(reason, 0) + 1
    actors = metrics.setdefault("rejections_by_role", {})
    actor = str(receipt.get("actor_id", "unknown"))
    actors[actor] = actors.get(actor, 0) + 1


def update_metrics(run: dict) -> None:
    """Ratios expose denominators, and missing observations stay unknown."""
    metrics = run["metrics"]
    proposed = metrics.get("commands_proposed", 0)
    accepted = metrics.get("commands_accepted", 0)
    rejected = metrics.get("commands_rejected", 0)
    metrics["command_acceptance_rate"] = accepted / proposed if proposed else None
    metrics["command_rejection_rate"] = rejected / proposed if proposed else None
    # This deliberately does not call a surviving idle unit an accepted-command completion.
    evaluation = run.get("evaluation") or {}
    items = run.get("milestones", [])
    metrics["milestones_achieved"] = sum(bool(item.get("achieved")) for item in items)
    metrics["milestones_total"] = len(items)
    metrics["milestone_completion_rate"] = metrics["milestones_achieved"] / len(items) if items else None
    stages = evaluation.get("stages", [])
    metrics["stages_completed"] = sum(bool(item.get("complete")) for item in stages)
    metrics["stages_total"] = len(stages)
    metrics["stage_completion_rate"] = metrics["stages_completed"] / len(stages) if stages else None
    metrics["task_completion_game_seconds"] = (
        run.get("observation", {}).get("game_time") if run.get("result") == "success" else None
    )
    metrics["task_completion_wall_seconds"] = metrics.get("wall_seconds") if run.get("result") == "success" else None
    metrics["task_success"] = run.get("result") == "success"
    score = run.get("observation", {}).get("score", {})
    metrics["native_score"] = dict(score) if isinstance(score, dict) else {}


def receipt_feedback(receipts: list[dict], role: str) -> list[dict]:
    """Keep this role's bounded most-recent outcomes; accepted is not completed."""
    return [receipt for receipt in receipts if receipt.get("actor_id") == role][-8:]

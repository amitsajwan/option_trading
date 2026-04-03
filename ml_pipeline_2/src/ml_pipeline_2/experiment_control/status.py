from __future__ import annotations

from typing import Any, Dict, Literal


RunIntegrity = Literal["clean", "resumed", "restarted", "contaminated", "unknown"]
RunLifecycleStatus = Literal["created", "running", "held", "completed", "failed", "abandoned", "restarted", "resumed"]


def infer_run_lifecycle_status(summary: Dict[str, Any]) -> RunLifecycleStatus:
    status = str(summary.get("status") or "").strip().lower()
    if status == "failed":
        return "failed"
    completion_mode = str(summary.get("completion_mode") or "").strip().lower()
    publish_assessment = dict(summary.get("publish_assessment") or {})
    if status == "completed" and not bool(publish_assessment.get("publishable", False)):
        return "held" if completion_mode and completion_mode != "completed" else "completed"
    return "completed"


def summary_execution_integrity(summary: Dict[str, Any]) -> RunIntegrity:
    raw = str(summary.get("execution_integrity") or "").strip().lower()
    if raw in {"clean", "resumed", "restarted", "contaminated", "unknown"}:
        return raw  # type: ignore[return-value]
    return "unknown"


def is_publish_integrity_ok(summary: Dict[str, Any]) -> bool:
    return summary_execution_integrity(summary) == "clean"


__all__ = [
    "RunIntegrity",
    "RunLifecycleStatus",
    "infer_run_lifecycle_status",
    "is_publish_integrity_ok",
    "summary_execution_integrity",
]

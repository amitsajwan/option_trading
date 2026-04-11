from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .spec import LaneSpec


class LaneOutcome(str, Enum):
    PUBLISHABLE = "publishable"
    HELD = "held"
    GATE_FAILED = "gate_failed"
    INFRA_FAILED = "infra_failed"


_GATE_FAILURE_COMPLETION_MODES = {
    "stage2_signal_check_failed",
    "stage1_cv_gate_failed",
    "stage2_cv_gate_failed",
}


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def summary_path_for_lane(lane: LaneSpec, run_dir: Path) -> Path:
    return Path(run_dir).resolve() / lane.summary_filename


def _join_reasons(values: Any) -> str:
    parts = [str(item).strip() for item in list(values or []) if str(item).strip()]
    return ", ".join(parts)


def _grid_blocking_reason(payload: Dict[str, Any], winner: Dict[str, Any]) -> str:
    reason = _join_reasons(winner.get("blocking_reasons"))
    if reason:
        return reason
    return str(payload.get("dominant_failure_reason") or winner.get("completion_mode") or "not_publishable")


def extract_metrics(*, lane_kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if lane_kind == "staged_grid":
        winner = dict(payload.get("winner") or {})
        combined = dict(winner.get("combined_holdout_summary") or {})
        stage2_cv = dict(winner.get("stage2_cv") or {})
        return {
            "profit_factor": combined.get("profit_factor"),
            "net_return_sum": combined.get("net_return_sum"),
            "trades": combined.get("trades"),
            "max_drawdown_pct": combined.get("max_drawdown_pct"),
            "stage2_roc_auc": stage2_cv.get("roc_auc"),
            "stage2_brier": stage2_cv.get("brier"),
            "winner_run_id": winner.get("grid_run_id"),
        }
    combined = dict((((payload.get("holdout_reports") or {}).get("stage3") or {}).get("combined_holdout_summary")) or {})
    stage2_cv = dict((payload.get("cv_prechecks") or {}).get("stage2_cv") or {})
    return {
        "profit_factor": combined.get("profit_factor"),
        "net_return_sum": combined.get("net_return_sum"),
        "trades": combined.get("trades"),
        "max_drawdown_pct": combined.get("max_drawdown_pct"),
        "stage2_roc_auc": stage2_cv.get("roc_auc"),
        "stage2_brier": stage2_cv.get("brier"),
        "run_id": payload.get("run_id"),
    }


def classify_lane_result(lane: LaneSpec, run_dir: Path, *, exit_code: Optional[int]) -> Tuple[LaneOutcome, Optional[Dict[str, Any]], Optional[Path], Optional[str]]:
    summary_path = summary_path_for_lane(lane, run_dir)
    if exit_code is not None and exit_code != 0 and not summary_path.exists():
        return LaneOutcome.INFRA_FAILED, None, None, f"runner exited with code {exit_code}"
    if not summary_path.exists():
        return LaneOutcome.INFRA_FAILED, None, None, "expected summary artifact not found"

    payload = _load_json(summary_path)
    if lane.lane_kind == "staged_grid":
        status = str(payload.get("status") or "").strip().lower()
        if status == "failed":
            error = dict(payload.get("error") or {})
            return LaneOutcome.INFRA_FAILED, None, summary_path, str(error.get("message") or "grid_failed")
        winner = dict(payload.get("winner") or {})
        if not winner:
            return LaneOutcome.GATE_FAILED, None, summary_path, str(payload.get("dominant_failure_reason") or "no_grid_winner")
        if bool(winner.get("publishable")):
            return LaneOutcome.PUBLISHABLE, extract_metrics(lane_kind=lane.lane_kind, payload=payload), summary_path, None
        completion_mode = str(winner.get("completion_mode") or "").strip().lower()
        reason = _grid_blocking_reason(payload, winner)
        if completion_mode in _GATE_FAILURE_COMPLETION_MODES:
            return LaneOutcome.GATE_FAILED, None, summary_path, reason
        return LaneOutcome.HELD, extract_metrics(lane_kind=lane.lane_kind, payload=payload), summary_path, reason

    status = str(payload.get("status") or "").strip().lower()
    if status == "failed":
        error = dict(payload.get("error") or {})
        return LaneOutcome.INFRA_FAILED, None, summary_path, str(error.get("message") or "staged_run_failed")
    publish_assessment = dict(payload.get("publish_assessment") or {})
    if bool(publish_assessment.get("publishable")):
        return LaneOutcome.PUBLISHABLE, extract_metrics(lane_kind=lane.lane_kind, payload=payload), summary_path, None

    completion_mode = str(payload.get("completion_mode") or "").strip().lower()
    if completion_mode in _GATE_FAILURE_COMPLETION_MODES:
        reason = _join_reasons(publish_assessment.get("blocking_reasons")) or completion_mode
        return LaneOutcome.GATE_FAILED, None, summary_path, reason
    return LaneOutcome.HELD, extract_metrics(lane_kind=lane.lane_kind, payload=payload), summary_path, _join_reasons(publish_assessment.get("blocking_reasons")) or "not_publishable"


__all__ = ["LaneOutcome", "classify_lane_result", "extract_metrics", "summary_path_for_lane"]

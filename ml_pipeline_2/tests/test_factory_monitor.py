from __future__ import annotations

import json
from pathlib import Path

from ml_pipeline_2.factory.monitor import LaneOutcome, classify_lane_result
from ml_pipeline_2.factory.spec import LaneSpec, ResourceSpec


def _lane(*, kind: str) -> LaneSpec:
    return LaneSpec(
        lane_id="lane1",
        lane_kind=kind,  # type: ignore[arg-type]
        config_path=Path("dummy.json"),
        runner_mode="research",
        depends_on=(),
        resource=ResourceSpec(cores=1, memory_gb=1),
        model_group="mg" if kind == "staged_grid" else None,
        profile_id="pf" if kind == "staged_grid" else None,
    )


def test_factory_monitor_classifies_missing_artifact_as_infra(tmp_path: Path) -> None:
    outcome, metrics, summary_path, error = classify_lane_result(_lane(kind="staged_manifest"), tmp_path, exit_code=1)
    assert outcome == LaneOutcome.INFRA_FAILED
    assert metrics is None
    assert summary_path is None
    assert "code 1" in str(error)


def test_factory_monitor_classifies_staged_gate_failure(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "completion_mode": "stage2_cv_gate_failed",
                "publish_assessment": {"publishable": False, "blocking_reasons": ["stage2_cv.roc_auc<0.55"]},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    outcome, metrics, _, error = classify_lane_result(_lane(kind="staged_manifest"), tmp_path, exit_code=0)
    assert outcome == LaneOutcome.GATE_FAILED
    assert metrics is None
    assert "stage2_cv" in str(error)


def test_factory_monitor_classifies_staged_publishable(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "completion_mode": "completed",
                "cv_prechecks": {"stage2_cv": {"roc_auc": 0.61, "brier": 0.21}},
                "holdout_reports": {"stage3": {"combined_holdout_summary": {"profit_factor": 1.6, "net_return_sum": 0.2, "trades": 80, "max_drawdown_pct": 0.08}}},
                "publish_assessment": {"publishable": True, "blocking_reasons": []},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    outcome, metrics, _, _ = classify_lane_result(_lane(kind="staged_manifest"), tmp_path, exit_code=0)
    assert outcome == LaneOutcome.PUBLISHABLE
    assert metrics["profit_factor"] == 1.6


def test_factory_monitor_classifies_grid_gate_failure(tmp_path: Path) -> None:
    (tmp_path / "grid_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "winner": {
                    "grid_run_id": "best",
                    "completion_mode": "stage2_cv_gate_failed",
                    "publishable": False,
                    "blocking_reasons": ["stage2_cv.roc_auc<0.55"],
                    "stage2_cv": {"roc_auc": 0.38, "brier": 0.29},
                    "combined_holdout_summary": {"profit_factor": 1.2, "net_return_sum": 0.02, "trades": 70, "max_drawdown_pct": 0.09},
                },
                "dominant_failure_reason": "stage2_cv.roc_auc<0.55",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    outcome, metrics, _, error = classify_lane_result(_lane(kind="staged_grid"), tmp_path, exit_code=0)
    assert outcome == LaneOutcome.GATE_FAILED
    assert metrics is None
    assert "stage2_cv" in str(error)


def test_factory_monitor_classifies_grid_held(tmp_path: Path) -> None:
    (tmp_path / "grid_summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "winner": {
                    "grid_run_id": "best",
                    "completion_mode": "completed",
                    "publishable": False,
                    "stage2_cv": {"roc_auc": 0.58, "brier": 0.22},
                    "combined_holdout_summary": {"profit_factor": 1.2, "net_return_sum": 0.02, "trades": 70, "max_drawdown_pct": 0.09},
                },
                "dominant_failure_reason": "combined_holdout.profit_factor<1.5",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    outcome, metrics, _, error = classify_lane_result(_lane(kind="staged_grid"), tmp_path, exit_code=0)
    assert outcome == LaneOutcome.HELD
    assert metrics["winner_run_id"] == "best"
    assert "profit_factor" in str(error)

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from strategy_app.main import _resolve_ml_pure_switch_paths


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _prepare_published_model(root: Path, *, run_id: str) -> None:
    group_root = root / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto"
    model_path = group_root / "data" / "training_runs" / run_id / "model" / "model.joblib"
    threshold_path = group_root / "data" / "training_runs" / run_id / "config" / "profiles" / "x" / "threshold_report.json"
    run_report_path = group_root / "reports" / "training" / f"run_{run_id}.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    threshold_path.parent.mkdir(parents=True, exist_ok=True)
    run_report_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "feature_columns": ["ret_5m"],
            "models": {"ce": _ConstantProbModel(0.8), "pe": _ConstantProbModel(0.2)},
        },
        model_path,
    )
    threshold_path.write_text(json.dumps({"ce_threshold": 0.6, "pe_threshold": 0.6}), encoding="utf-8")
    run_report_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "publish_status": "published",
                "publish_decision": {"decision": "PUBLISH"},
                "published_paths": {
                    "model_package": str(model_path.relative_to(root)).replace("\\", "/"),
                    "threshold_report": str(threshold_path.relative_to(root)).replace("\\", "/"),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_strategy_switch_prefers_ml_pipeline_2_published_runs(tmp_path: Path, monkeypatch) -> None:
    _prepare_published_model(tmp_path, run_id="run_20260313_010101")
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    model_package, threshold_report, meta = _resolve_ml_pure_switch_paths(
        engine_key="ml_pure",
        run_id="run_20260313_010101",
        model_group="banknifty_futures/h15_tp_auto",
        model_package=None,
        threshold_report=None,
    )

    assert model_package is not None
    assert threshold_report is not None
    assert meta is not None
    assert "ml_pipeline_2/artifacts/published_models" in model_package.replace("\\", "/")
    assert meta["run_id"] == "run_20260313_010101"

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml_pipeline_2.publishing import resolve_ml_pure_artifacts, validate_switch_strict
from ml_pipeline_2.staged.publish import publish_staged_run


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _single_target_package(*, prob: float, model_key: str, prob_col: str) -> dict[str, object]:
    return {
        "feature_columns": ["fut_return_5m", "pcr"],
        "_model_input_contract": {
            "required_features": ["fut_return_5m", "pcr"],
            "allow_extra_features": True,
            "missing_policy": "error",
            "contract_id": "snapshot_stage_view",
        },
        "single_target": {"model_key": model_key, "prob_col": prob_col},
        "models": {model_key: _ConstantProbModel(prob)},
    }


def test_publish_staged_run_writes_runtime_bundle_and_resolver_can_switch(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_publish_fixture_20260318_010101"
    stage1_root = run_dir / "stages" / "stage1"
    stage2_root = run_dir / "stages" / "stage2"
    stage3_root = run_dir / "stages" / "stage3" / "recipes"
    detached_reports_root = run_dir / "stage3_reports"
    stage1_root.mkdir(parents=True, exist_ok=True)
    stage2_root.mkdir(parents=True, exist_ok=True)
    (run_dir / "stages" / "stage3").mkdir(parents=True, exist_ok=True)
    detached_reports_root.mkdir(parents=True, exist_ok=True)
    joblib.dump(_single_target_package(prob=0.8, model_key="move", prob_col="move_prob"), stage1_root / "model.joblib")
    joblib.dump(_single_target_package(prob=0.7, model_key="direction", prob_col="direction_up_prob"), stage2_root / "model.joblib")
    for recipe_id, prob in {"L0": 0.75, "L1": 0.40}.items():
        recipe_root = stage3_root / recipe_id
        recipe_root.mkdir(parents=True, exist_ok=True)
        joblib.dump(_single_target_package(prob=prob, model_key="move", prob_col="move_prob"), recipe_root / "model.joblib")

    summary = {
        "status": "completed",
        "experiment_kind": "staged_dual_recipe_v1",
        "run_id": run_dir.name,
        "recipe_catalog_id": "fixed_l0_l3_v1",
        "publish_assessment": {"decision": "PUBLISH", "publishable": True, "blocking_reasons": []},
        "runtime_prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1"],
        "policy_reports": {
            "stage1": {"policy_id": "entry_threshold_v1", "selected_threshold": 0.55},
            "stage2": {"policy_id": "direction_dual_threshold_v1", "selected_ce_threshold": 0.60, "selected_pe_threshold": 0.60, "selected_min_edge": 0.10},
            "stage3": {"policy_id": "recipe_top_margin_v1", "selected_threshold": 0.60, "selected_margin_min": 0.10},
        },
        "component_ids": {
            "stage1": {"view_id": "stage1_entry_view_v1"},
            "stage2": {"view_id": "stage2_direction_view_v1"},
            "stage3": {"view_id": "stage3_recipe_view_v1"},
        },
        "stage_artifacts": {
            "stage1": {"model_package_path": str((stage1_root / "model.joblib").resolve())},
            "stage2": {"model_package_path": str((stage2_root / "model.joblib").resolve())},
            "stage3": {
                "training_report_path": str((detached_reports_root / "training_report.json").resolve()),
                "recipes": ["L0", "L1"],
                "recipe_artifacts": {
                    "L0": {"model_package_path": str((stage3_root / "L0" / "model.joblib").resolve())},
                    "L1": {"model_package_path": str((stage3_root / "L1" / "model.joblib").resolve())},
                },
            },
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    payload = publish_staged_run(
        run_dir=run_dir,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert payload["publish_status"] == "published"
    resolved = resolve_ml_pure_artifacts(run_dir.name, "banknifty_futures/h15_tp_auto")
    ok, reason = validate_switch_strict(dict(resolved["run_report_payload"]))
    assert ok, reason
    threshold_report = json.loads(Path(str(resolved["threshold_report_path"])).read_text(encoding="utf-8"))
    assert threshold_report["kind"] == "ml_pipeline_2_staged_runtime_policy_v1"
    runtime_bundle = joblib.load(Path(str(resolved["model_package_path"])))
    assert runtime_bundle["stages"]["stage1"]["view_name"] == "stage1_entry_view"
    assert runtime_bundle["stages"]["stage2"]["view_name"] == "stage2_direction_view"
    assert sorted(runtime_bundle["stages"]["stage3"]["recipe_packages"]) == ["L0", "L1"]

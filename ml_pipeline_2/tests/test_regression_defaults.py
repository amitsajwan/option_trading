from __future__ import annotations

import json
from pathlib import Path

from ml_pipeline_2.catalog import default_phase2_manifest_payload, default_recovery_manifest_payload


def test_default_phase2_manifest_matches_current_defaults() -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json").read_text(encoding="utf-8"))
    assert payload["catalog"]["feature_sets"] == ["fo_expiry_aware_v2"]
    assert default_phase2_manifest_payload()["catalog"]["feature_sets"] == ["fo_expiry_aware_v2"]
    assert payload["inputs"] == {
        "model_window_features_path": "../../../.data/ml_pipeline/frozen/model_window_features.parquet",
        "holdout_features_path": "../../../.data/ml_pipeline/frozen/holdout_features.parquet",
        "base_path": "../../../.data/ml_pipeline",
    }
    assert payload["outputs"]["artifacts_root"] == "../../artifacts/research"
    assert payload["windows"]["research_train"] == {"start": "2020-08-03", "end": "2024-04-30"}
    assert payload["windows"]["research_valid"] == {"start": "2024-05-01", "end": "2024-07-31"}
    assert payload["windows"]["full_model"] == {"start": "2020-08-03", "end": "2024-07-31"}
    assert payload["windows"]["final_holdout"] == {"start": "2024-08-01", "end": "2024-10-31"}
    assert payload["scenario"]["threshold_grid"] == [0.25, 0.3, 0.35]
    assert payload["scenario"]["stress_models"] == ["xgb_shallow", "lgbm_dart", "logreg_balanced"]
    assert [recipe["recipe_id"] for recipe in payload["scenario"]["recipes"]] == ["L0", "L1", "L2", "L3"]


def test_default_recovery_manifest_matches_current_defaults() -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json").read_text(encoding="utf-8"))
    assert payload["catalog"]["feature_sets"] == ["fo_expiry_aware_v2"]
    assert default_recovery_manifest_payload()["catalog"]["feature_sets"] == ["fo_expiry_aware_v2"]
    assert payload["inputs"] == {
        "model_window_features_path": "../../../.data/ml_pipeline/frozen/model_window_features.parquet",
        "holdout_features_path": "../../../.data/ml_pipeline/frozen/holdout_features.parquet",
        "base_path": "../../../.data/ml_pipeline",
        "baseline_json_path": "",
    }
    assert payload["outputs"]["artifacts_root"] == "../../artifacts/research"
    assert payload["windows"]["full_model"] == {"start": "2020-08-03", "end": "2024-07-31"}
    assert payload["windows"]["final_holdout"] == {"start": "2024-08-01", "end": "2024-10-31"}
    assert payload["training"]["cv_config"] == {"train_days": 180, "valid_days": 30, "test_days": 30, "step_days": 30, "purge_days": 0, "embargo_days": 0, "purge_mode": "event_overlap", "embargo_rows": 5, "event_end_col": None}
    assert payload["scenario"]["primary_threshold"] == 0.25
    assert [(recipe["recipe_id"], recipe["barrier_mode"]) for recipe in payload["scenario"]["recipes"]] == [("TB_BASE_L3", "fixed"), ("TB_ATR_L3", "atr_scaled"), ("TB_BASE_L1", "fixed"), ("TB_ATR_L1", "atr_scaled")]

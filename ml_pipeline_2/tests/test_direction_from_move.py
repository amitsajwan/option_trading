from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml_pipeline_2.inference_contract import predict_probabilities_from_frame
from ml_pipeline_2.labeling import EffectiveLabelConfig, build_labeled_dataset
from ml_pipeline_2.model_search import select_feature_columns
from ml_pipeline_2.model_search import ConstantProbModel, run_training_cycle_catalog
from ml_pipeline_2.run_direction_from_move_quick import run_direction_from_move_quick
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def test_direction_target_trains_single_model_package(tmp_path: Path) -> None:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    cfg = EffectiveLabelConfig(
        horizon_minutes=2,
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        take_profit_pct=0.0010,
        stop_loss_pct=0.0005,
    )
    labeled = build_labeled_dataset(features=features, cfg=cfg)
    result = run_training_cycle_catalog(
        labeled_df=labeled,
        feature_profile="all",
        objective="brier",
        train_days=4,
        valid_days=2,
        test_days=2,
        step_days=2,
        random_state=42,
        max_experiments=1,
        model_whitelist=["logreg_balanced"],
        feature_set_whitelist=["fo_expiry_aware"],
        label_target="move_direction_up",
    )
    package = result["model_package"]
    assert package["prediction_mode"] == "direction_up"
    assert package["single_target"]["model_key"] == "direction"
    probs, _ = predict_probabilities_from_frame(labeled, package, context="direction-up-unit")
    assert list(probs.columns) == ["direction_up_prob"]


def test_direction_runner_resume_returns_existing_summary(tmp_path: Path) -> None:
    stage1_run_dir = tmp_path / "stage1"
    stage1_run_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "inputs": {"stage1_run_dir": str(stage1_run_dir)},
        "training": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware"],
            "models": ["logreg_balanced"],
            "max_experiments": 1,
            "objective": "brier",
            "cv": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2},
        },
        "gating": {"move_threshold": 0.6, "direction_threshold_grid": [0.6], "cost_per_trade": 0.0006},
        "outputs": {
            "out_root": str(tmp_path / "artifacts"),
            "run_name": "direction_from_move_quick",
            "run_dir": str(tmp_path / "artifacts" / "resume_case"),
            "resume": True,
        },
    }
    config_path = tmp_path / "direction_config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    run_dir = Path(config["outputs"]["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "resolved_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    summary_payload = {"status": "completed", "output_root": str(run_dir), "holdout_direction_quality": {"rows_move_positive": 11}}
    (run_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    summary = run_direction_from_move_quick(["--config", str(config_path), "--resume"])
    assert summary == summary_payload


def test_direction_inference_contract_supports_direction_only_package(tmp_path: Path) -> None:
    _, holdout_path = build_synthetic_feature_frames(tmp_path)
    holdout = pd.read_parquet(holdout_path)
    package = {
        "feature_columns": ["ret_1m", "osc_atr_ratio"],
        "models": {"direction": ConstantProbModel(0.65)},
        "single_target": {"model_key": "direction", "prob_col": "direction_up_prob", "prediction_mode": "direction_up", "event_end_col": "move_event_end_ts"},
        "_model_input_contract": {
            "required_features": ["ret_1m", "osc_atr_ratio"],
            "missing_policy": "error",
            "source": "feature_columns",
        },
    }
    probs, validation = predict_probabilities_from_frame(holdout, package, context="direction-only-unit")
    assert validation["missing_required_count"] == 0
    assert list(probs.columns) == ["direction_up_prob"]


def test_label_derived_numeric_columns_are_excluded_from_feature_selection(tmp_path: Path) -> None:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    cfg = EffectiveLabelConfig(
        horizon_minutes=2,
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        take_profit_pct=0.0010,
        stop_loss_pct=0.0005,
    )
    labeled = build_labeled_dataset(features=features, cfg=cfg)
    selected = set(select_feature_columns(labeled, feature_profile="all"))
    forbidden = {
        "ce_triple_barrier_state",
        "ce_barrier_upper_return",
        "ce_barrier_lower_return",
        "ce_hold_extension_eligible",
        "pe_triple_barrier_state",
        "pe_barrier_upper_return",
        "pe_barrier_lower_return",
        "pe_hold_extension_eligible",
        "long_triple_barrier_state",
        "short_triple_barrier_state",
        "move_barrier_upper_return",
        "move_barrier_lower_return",
    }
    assert selected.isdisjoint(forbidden)

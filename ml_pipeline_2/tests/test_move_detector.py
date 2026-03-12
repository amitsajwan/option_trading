from __future__ import annotations

from pathlib import Path

import json
import pandas as pd

from ml_pipeline_2.inference_contract import predict_probabilities_from_frame
from ml_pipeline_2.labeling import EffectiveLabelConfig, build_labeled_dataset
from ml_pipeline_2.model_search import ConstantProbModel, run_training_cycle_catalog
from ml_pipeline_2.run_move_detector_quick import run_move_detector_quick
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def test_move_label_columns_are_emitted(tmp_path: Path) -> None:
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
    for col in ("move_label_valid", "move_label", "move_path_exit_reason", "move_first_hit_side", "move_event_end_ts"):
        assert col in labeled.columns
    valid = labeled[pd.to_numeric(labeled["move_label_valid"], errors="coerce").fillna(0.0) == 1.0]
    assert set(valid["move_first_hit_side"].astype(str).unique()).issubset({"up", "down", "none", "invalid"})
    assert set(pd.to_numeric(valid["move_label"], errors="coerce").dropna().astype(int).unique()).issubset({0, 1})


def test_inference_contract_supports_move_only_package(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    holdout = pd.read_parquet(holdout_path)
    package = {
        "feature_columns": ["ret_1m", "osc_atr_ratio"],
        "models": {"move": ConstantProbModel(0.7)},
        "_model_input_contract": {
            "required_features": ["ret_1m", "osc_atr_ratio"],
            "missing_policy": "error",
            "source": "feature_columns",
        },
    }
    probs, validation = predict_probabilities_from_frame(holdout, package, context="move-only-unit")
    assert validation["missing_required_count"] == 0
    assert list(probs.columns) == ["move_prob"]
    assert len(probs) == len(holdout)


def test_move_target_rejects_trade_utility_objective(tmp_path: Path) -> None:
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
    try:
        run_training_cycle_catalog(
            labeled_df=labeled,
            feature_profile="all",
            objective="trade_utility",
            train_days=4,
            valid_days=2,
            test_days=2,
            step_days=2,
            random_state=42,
            max_experiments=1,
            model_whitelist=["logreg_balanced"],
            feature_set_whitelist=["fo_expiry_aware"],
            label_target="move_barrier_hit",
        )
    except ValueError as exc:
        assert "does not support trade_utility" in str(exc)
    else:
        raise AssertionError("expected move_barrier_hit to reject trade_utility objective")


def test_move_detector_resume_returns_existing_summary(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    run_dir = tmp_path / "artifacts" / "resume_case"
    config = {
        "inputs": {
            "model_window_features": str(model_window_path),
            "holdout_features": str(holdout_path),
        },
        "windows": {
            "train": {"start": "2024-01-01", "end": "2024-01-12"},
            "holdout": {"start": "2024-01-13", "end": "2024-01-18"},
        },
        "label": {
            "horizon_minutes": 2,
            "atr_multiplier": 1.0,
            "fallback_barrier_pct": 0.0010,
            "min_entry_time": "09:20",
        },
        "training": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware"],
            "models": ["logreg_balanced"],
            "max_experiments": 1,
            "objective": "brier",
            "threshold_grid": [0.5],
            "cv": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2},
        },
        "outputs": {
            "out_root": str(tmp_path / "artifacts"),
            "run_name": "move_detector_quick",
            "run_dir": str(run_dir),
            "resume": True,
        },
    }
    config_path = tmp_path / "move_detector_config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "resolved_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    summary_payload = {"status": "completed", "output_root": str(run_dir), "holdout_metrics": {"rows_valid": 7}}
    (run_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    summary = run_move_detector_quick(["--config", str(config_path), "--resume"])
    assert summary == summary_payload

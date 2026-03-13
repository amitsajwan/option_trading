from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.labeling import EffectiveLabelConfig, build_labeled_dataset, prepare_snapshot_labeled_frame
from ml_pipeline_2.model_search import run_training_cycle_catalog
from ml_pipeline_2.contracts.types import PreprocessConfig, TradingObjectiveConfig
from ml_pipeline_2.tests.helpers import build_phase2_smoke_manifest, build_recovery_smoke_manifest, build_synthetic_feature_frames


def test_phase2_smoke_runs_end_to_end(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_phase2_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    output_root = Path(summary["output_root"])
    assert (output_root / "resolved_config.json").exists()
    assert (output_root / "manifest_hash.txt").exists()
    assert (output_root / "state.jsonl").exists()
    assert (output_root / "phase2_summary.json").exists()
    assert (output_root / "phase2_binary_baseline.json").exists()
    assert (output_root / "recipes" / "L1" / "selection_summary.json").exists()
    assert (output_root / "model_stress" / "L1" / "model_stress_summary.json").exists()


def test_recovery_smoke_runs_end_to_end(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    output_root = Path(summary["output_root"])
    assert (output_root / "resolved_config.json").exists()
    assert (output_root / "manifest_hash.txt").exists()
    assert (output_root / "state.jsonl").exists()
    assert (output_root / "summary.json").exists()
    assert (output_root / "primary_recipes" / "TB_BASE_L1" / "summary.json").exists()
    assert (output_root / "meta_gate" / "summary.json").exists()


def _build_training_cycle_smoke_frame(tmp_path: Path) -> pd.DataFrame:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    labeled = build_labeled_dataset(
        features=features.copy(),
        cfg=EffectiveLabelConfig(
            horizon_minutes=2,
            return_threshold=0.0,
            use_excursion_gate=False,
            min_favorable_excursion=0.0,
            max_adverse_excursion=0.0,
            stop_loss_pct=0.0005,
            take_profit_pct=0.0010,
            allow_hold_extension=False,
            extension_trigger_profit_pct=0.0,
        ),
    )
    return prepare_snapshot_labeled_frame(labeled, context="smoke:tuning")


def test_training_cycle_smoke_supports_xgb_deep_v1(tmp_path: Path) -> None:
    labeled = _build_training_cycle_smoke_frame(tmp_path)

    result = run_training_cycle_catalog(
        labeled_df=labeled,
        feature_profile="all",
        objective="trade_utility",
        train_days=4,
        valid_days=2,
        test_days=2,
        step_days=2,
        purge_days=0,
        embargo_days=0,
        purge_mode="days",
        embargo_rows=0,
        event_end_col=None,
        random_state=42,
        max_experiments=1,
        preprocess_cfg=PreprocessConfig(),
        label_target="path_tp_sl_resolved_only",
        utility_cfg=TradingObjectiveConfig(
            ce_threshold=0.50,
            pe_threshold=0.50,
            min_profit_factor=0.50,
            max_equity_drawdown_pct=0.50,
            min_trades=1,
            take_profit_pct=0.0010,
            stop_loss_pct=0.0005,
        ),
        model_whitelist=["xgb_deep_v1"],
        feature_set_whitelist=["fo_expiry_aware_v2"],
        fit_all_final_models=False,
    )

    assert result["report"]["best_experiment"]["model"]["name"] == "xgb_deep_v1"
    assert result["model_package"]["selected_model"]["name"] == "xgb_deep_v1"


def test_training_cycle_smoke_supports_lgbm_large_v1(tmp_path: Path) -> None:
    pytest.importorskip("lightgbm")
    labeled = _build_training_cycle_smoke_frame(tmp_path)

    result = run_training_cycle_catalog(
        labeled_df=labeled,
        feature_profile="all",
        objective="trade_utility",
        train_days=4,
        valid_days=2,
        test_days=2,
        step_days=2,
        purge_days=0,
        embargo_days=0,
        purge_mode="days",
        embargo_rows=0,
        event_end_col=None,
        random_state=42,
        max_experiments=1,
        preprocess_cfg=PreprocessConfig(),
        label_target="path_tp_sl_resolved_only",
        utility_cfg=TradingObjectiveConfig(
            ce_threshold=0.50,
            pe_threshold=0.50,
            min_profit_factor=0.50,
            max_equity_drawdown_pct=0.50,
            min_trades=1,
            take_profit_pct=0.0010,
            stop_loss_pct=0.0005,
        ),
        model_whitelist=["lgbm_large_v1"],
        feature_set_whitelist=["fo_expiry_aware_v2"],
        fit_all_final_models=False,
    )

    assert result["report"]["best_experiment"]["model"]["name"] == "lgbm_large_v1"
    assert result["model_package"]["selected_model"]["name"] == "lgbm_large_v1"

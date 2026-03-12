from __future__ import annotations

from pathlib import Path

import pandas as pd

from ml_pipeline_2.labeling import EffectiveLabelConfig, build_labeled_dataset, prepare_snapshot_labeled_frame
from ml_pipeline_2.model_search import run_training_cycle_catalog
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def test_fo_expiry_aware_v2_includes_regime_features_missing_from_v1(tmp_path: Path) -> None:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    trade_dates = pd.to_datetime(features["trade_date"], errors="coerce")
    features["osc_atr_daily_percentile"] = ((trade_dates.dt.day % 2) == 0).astype(float) * 0.70 + 0.15
    cfg = EffectiveLabelConfig(
        horizon_minutes=2,
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        take_profit_pct=0.0010,
        stop_loss_pct=0.0005,
    )
    labeled = prepare_snapshot_labeled_frame(
        build_labeled_dataset(features=features, cfg=cfg),
        context="feature-set-unit",
    )

    v1_result = run_training_cycle_catalog(
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
        label_target="move_barrier_hit",
    )
    v2_result = run_training_cycle_catalog(
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
        feature_set_whitelist=["fo_expiry_aware_v2"],
        label_target="move_barrier_hit",
    )

    v1_features = set(v1_result["model_package"]["feature_columns"])
    v2_features = set(v2_result["model_package"]["feature_columns"])

    assert "regime_vol_high" not in v1_features
    assert "regime_atr_high" not in v1_features
    assert "regime_trend_up" not in v1_features
    assert {"regime_vol_high", "regime_atr_high", "regime_atr_low", "regime_trend_up", "regime_trend_down"}.issubset(v2_features)


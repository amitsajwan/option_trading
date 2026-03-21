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


def test_fo_expiry_aware_v3_includes_stage2_direction_delta_features(tmp_path: Path) -> None:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    trade_dates = pd.to_datetime(features["trade_date"], errors="coerce")
    features["osc_atr_daily_percentile"] = ((trade_dates.dt.day % 2) == 0).astype(float) * 0.70 + 0.15
    features["pcr"] = 0.95 + (trade_dates.dt.day % 3).astype(float) * 0.01
    features["pcr_change_5m"] = (trade_dates.dt.day % 4).astype(float) * 0.01
    features["pcr_change_15m"] = (trade_dates.dt.day % 5).astype(float) * 0.01
    features["atm_oi_ratio"] = 0.45 + (trade_dates.dt.day % 3).astype(float) * 0.02
    features["near_atm_oi_ratio"] = 0.44 + (trade_dates.dt.day % 4).astype(float) * 0.02
    features["atm_ce_oi"] = 100000.0 + (trade_dates.dt.day % 5).astype(float) * 1000.0
    features["atm_pe_oi"] = 110000.0 + (trade_dates.dt.day % 5).astype(float) * 1000.0
    features["atm_ce_iv"] = 0.18 + (trade_dates.dt.day % 4).astype(float) * 0.005
    features["atm_pe_iv"] = 0.19 + (trade_dates.dt.day % 4).astype(float) * 0.005
    features["iv_skew"] = features["atm_pe_iv"] - features["atm_ce_iv"]
    features["iv_percentile"] = 0.30 + (trade_dates.dt.day % 4).astype(float) * 0.05
    features["vix_current"] = 14.0 + (trade_dates.dt.day % 6).astype(float) * 0.1
    features["vix_intraday_chg"] = (trade_dates.dt.day % 5).astype(float) * 0.02
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
        context="feature-set-unit-v3",
    )

    v3_result = run_training_cycle_catalog(
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
        feature_set_whitelist=["fo_expiry_aware_v3"],
        label_target="move_barrier_hit",
    )

    v3_features = set(v3_result["model_package"]["feature_columns"])
    assert {
        "pcr",
        "pcr_change_5m",
        "pcr_change_15m",
        "atm_oi_ratio",
        "near_atm_oi_ratio",
        "atm_ce_oi",
        "atm_pe_oi",
        "atm_ce_iv",
        "atm_pe_iv",
        "iv_skew",
        "iv_percentile",
        "vix_current",
        "vix_intraday_chg",
    }.issubset(v3_features)


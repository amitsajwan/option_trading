from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ml_pipeline_2.evaluation import FuturesPromotionGates, evaluate_futures_stages_from_frame, stage_b
from ml_pipeline_2.inference_contract import validate_model_input_columns
from ml_pipeline_2.labeling import EffectiveLabelConfig, build_label_lineage, build_labeled_dataset
from ml_pipeline_2.model_search import run_training_cycle_catalog
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def _load_labeled_training_frame(tmp_path: Path) -> pd.DataFrame:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    cfg = EffectiveLabelConfig(horizon_minutes=2, return_threshold=0.0, use_excursion_gate=False, min_favorable_excursion=0.0, max_adverse_excursion=0.0, take_profit_pct=0.0010, stop_loss_pct=0.0005)
    return build_labeled_dataset(features=features, cfg=cfg)


def test_labeling_lineage_is_stable(tmp_path: Path) -> None:
    labeled = _load_labeled_training_frame(tmp_path)
    cfg = EffectiveLabelConfig(horizon_minutes=2, return_threshold=0.0, use_excursion_gate=False, min_favorable_excursion=0.0, max_adverse_excursion=0.0, take_profit_pct=0.0010, stop_loss_pct=0.0005)
    lineage_a = build_label_lineage(labeled, cfg)
    lineage_b = build_label_lineage(labeled, cfg)
    assert lineage_a == lineage_b


def test_model_search_search_space_metadata_is_deterministic(tmp_path: Path) -> None:
    labeled = _load_labeled_training_frame(tmp_path)
    run_a = run_training_cycle_catalog(labeled_df=labeled, feature_profile="all", objective="trade_utility", train_days=4, valid_days=2, test_days=2, step_days=2, random_state=42, max_experiments=1, model_whitelist=["logreg_balanced"], feature_set_whitelist=["fo_expiry_aware"], label_target="path_tp_sl_time_stop_zero")
    run_b = run_training_cycle_catalog(labeled_df=labeled, feature_profile="all", objective="trade_utility", train_days=4, valid_days=2, test_days=2, step_days=2, random_state=42, max_experiments=1, model_whitelist=["logreg_balanced"], feature_set_whitelist=["fo_expiry_aware"], label_target="path_tp_sl_time_stop_zero")
    assert run_a["report"]["leaderboard"] == run_b["report"]["leaderboard"]


def test_inference_contract_rejects_missing_required_features() -> None:
    package = {"feature_columns": ["a", "b"], "models": {}, "_model_input_contract": {"required_features": ["a", "b"], "missing_policy": "error", "source": "feature_columns"}}
    with pytest.raises(ValueError):
        validate_model_input_columns(["a"], package, context="unit-test")


def test_evaluation_reports_expected_viability_shape() -> None:
    frame = pd.DataFrame({"long_label": [1, 0] * 10, "long_label_valid": [1.0] * 20, "short_label": [0, 1] * 10, "short_label_valid": [1.0] * 20, "long_forward_return": [0.01, -0.01] * 10, "short_forward_return": [-0.01, 0.01] * 10})
    probs = pd.DataFrame({"ce_prob": [0.9, 0.1] * 10, "pe_prob": [0.1, 0.9] * 10})
    gates = FuturesPromotionGates(long_roc_auc_min=0.5, short_roc_auc_min=0.5, brier_max=0.3, roc_auc_drift_max_abs=1.0, futures_pf_min=0.5, futures_max_drawdown_pct_max=1.0, futures_trades_min=1, side_share_min=0.2, side_share_max=0.8, block_rate_min=0.0)
    report = evaluate_futures_stages_from_frame(frame=frame, probs=probs, ce_threshold=0.5, pe_threshold=0.5, cost_per_trade=0.0001, gates=gates)
    raw_stage_b = stage_b(frame=frame, probs=probs, ce_threshold=0.5, pe_threshold=0.5, cost_per_trade=0.0001, gates=gates)
    assert report["stage_a_predictive_quality"]["passed"] is True
    assert raw_stage_b["profit_factor"] > 1.0

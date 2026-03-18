from __future__ import annotations

from typing import Any, Dict

from ..contracts.types import LabelRecipe, PreprocessConfig, RecoveryRecipe, TradingObjectiveConfig


DEFAULT_EXTERNAL_DATA_ROOT = "../../../.data/ml_pipeline"
DEFAULT_MODEL_WINDOW_FEATURES = f"{DEFAULT_EXTERNAL_DATA_ROOT}/frozen/model_window_features.parquet"
DEFAULT_HOLDOUT_FEATURES = f"{DEFAULT_EXTERNAL_DATA_ROOT}/frozen/holdout_features.parquet"
DEFAULT_STAGED_PARQUET_ROOT = f"{DEFAULT_EXTERNAL_DATA_ROOT}/parquet_data"


DEFAULT_PHASE2_RECIPES = (
    LabelRecipe(recipe_id="L0", horizon_minutes=15, take_profit_pct=0.0025, stop_loss_pct=0.0008),
    LabelRecipe(recipe_id="L1", horizon_minutes=15, take_profit_pct=0.0020, stop_loss_pct=0.0008),
    LabelRecipe(recipe_id="L2", horizon_minutes=15, take_profit_pct=0.0020, stop_loss_pct=0.0010),
    LabelRecipe(recipe_id="L3", horizon_minutes=20, take_profit_pct=0.0025, stop_loss_pct=0.0010),
)

DEFAULT_RECOVERY_RECIPES = (
    RecoveryRecipe("TB_BASE_L3", 20, 0.0025, 0.0010, "fixed"),
    RecoveryRecipe("TB_ATR_L3", 20, 0.0025, 0.0010, "atr_scaled"),
    RecoveryRecipe("TB_BASE_L1", 15, 0.0020, 0.0008, "fixed"),
    RecoveryRecipe("TB_ATR_L1", 15, 0.0020, 0.0008, "atr_scaled"),
)


def default_phase2_manifest_payload() -> Dict[str, Any]:
    preprocess = PreprocessConfig()
    utility = TradingObjectiveConfig(
        ce_threshold=0.30,
        pe_threshold=0.30,
        cost_per_trade=0.0006,
        min_profit_factor=1.50,
        max_equity_drawdown_pct=0.10,
        min_trades=50,
        take_profit_pct=0.0025,
        stop_loss_pct=0.0008,
        discard_time_stop=False,
        risk_per_trade_pct=0.01,
    )
    return {
        "schema_version": 1,
        "experiment_kind": "phase2_label_sweep_v1",
        "inputs": {
            "model_window_features_path": DEFAULT_MODEL_WINDOW_FEATURES,
            "holdout_features_path": DEFAULT_HOLDOUT_FEATURES,
            "base_path": DEFAULT_EXTERNAL_DATA_ROOT,
        },
        "outputs": {
            "artifacts_root": "../../artifacts/research",
            "run_name": "label_sweep_fo_expiry_aware_4y",
        },
        "catalog": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware_v2"],
            "models": ["xgb_shallow", "lgbm_dart", "logreg_balanced"],
        },
        "windows": {
            "research_train": {"start": "2020-08-03", "end": "2024-04-30"},
            "research_valid": {"start": "2024-05-01", "end": "2024-07-31"},
            "full_model": {"start": "2020-08-03", "end": "2024-07-31"},
            "final_holdout": {"start": "2024-08-01", "end": "2024-10-31"},
        },
        "training": {
            "objective": "trade_utility",
            "label_target": "path_tp_sl_time_stop_zero",
            "preprocess": preprocess.to_dict(),
            "cv_config": {
                "train_days": 84,
                "valid_days": 21,
                "test_days": 21,
                "step_days": 21,
                "purge_days": 0,
                "embargo_days": 0,
                "purge_mode": "days",
                "embargo_rows": 0,
                "event_end_col": None,
            },
            "utility": utility.to_dict(),
        },
        "scenario": {
            "recipes": [recipe.to_dict() for recipe in DEFAULT_PHASE2_RECIPES],
            "threshold_grid": [0.25, 0.30, 0.35],
            "default_model": "xgb_shallow",
            "stress_models": ["xgb_shallow", "lgbm_dart", "logreg_balanced"],
            "baseline_recipe_ids": ["L3", "L1"],
            "acceptance": {
                "holdout_side_share_min": 0.35,
                "holdout_side_share_max": 0.65,
            },
            "evaluation_gates": {},
        },
    }


def default_recovery_manifest_payload() -> Dict[str, Any]:
    preprocess = PreprocessConfig()
    utility = TradingObjectiveConfig(
        ce_threshold=0.25,
        pe_threshold=0.25,
        cost_per_trade=0.0006,
        min_profit_factor=1.10,
        max_equity_drawdown_pct=0.20,
        min_trades=25,
        take_profit_pct=0.0025,
        stop_loss_pct=0.0010,
        discard_time_stop=False,
        risk_per_trade_pct=0.01,
    )
    return {
        "schema_version": 1,
        "experiment_kind": "fo_expiry_aware_recovery_v1",
        "inputs": {
            "model_window_features_path": DEFAULT_MODEL_WINDOW_FEATURES,
            "holdout_features_path": DEFAULT_HOLDOUT_FEATURES,
            "base_path": DEFAULT_EXTERNAL_DATA_ROOT,
            "baseline_json_path": "",
        },
        "outputs": {
            "artifacts_root": "../../artifacts/research",
            "run_name": "fo_expiry_aware_recovery",
        },
        "catalog": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware_v2"],
            "models": ["xgb_shallow"],
        },
        "windows": {
            "full_model": {"start": "2020-08-03", "end": "2024-07-31"},
            "final_holdout": {"start": "2024-08-01", "end": "2024-10-31"},
        },
        "training": {
            "objective": "trade_utility",
            "label_target": "path_tp_sl_resolved_only",
            "preprocess": preprocess.to_dict(),
            "cv_config": {
                "train_days": 180,
                "valid_days": 30,
                "test_days": 30,
                "step_days": 30,
                "purge_days": 0,
                "embargo_days": 0,
                "purge_mode": "event_overlap",
                "embargo_rows": 5,
                "event_end_col": None,
            },
            "utility": utility.to_dict(),
        },
        "scenario": {
            "recipes": [recipe.to_dict() for recipe in DEFAULT_RECOVERY_RECIPES],
            "event_sampling_mode": "none",
            "event_signal_col": "opt_flow_ce_pe_oi_diff",
            "primary_model": "xgb_shallow",
            "primary_threshold": 0.25,
            "meta_gate": {
                "enabled": True,
                "validation_threshold_grid": [0.50, 0.55, 0.60, 0.65, 0.70],
            },
            "resume_primary": False,
            "recipe_selection": [],
            "evaluation_gates": {},
        },
    }


def default_staged_manifest_payload() -> Dict[str, Any]:
    preprocess = PreprocessConfig()
    return {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {
            "parquet_root": DEFAULT_STAGED_PARQUET_ROOT,
            "support_dataset": "snapshots_ml_flat",
        },
        "outputs": {
            "artifacts_root": "../../artifacts/research",
            "run_name": "staged_dual_recipe",
        },
        "catalog": {
            "models_by_stage": {
                "stage1": ["xgb_shallow", "lgbm_dart", "logreg_balanced"],
                "stage2": ["xgb_shallow", "lgbm_dart", "logreg_balanced"],
                "stage3": ["xgb_shallow", "lgbm_dart", "logreg_balanced"],
            },
            "feature_sets_by_stage": {
                "stage1": ["fo_expiry_aware_v2"],
                "stage2": ["fo_expiry_aware_v2"],
                "stage3": ["fo_full"],
            },
            "recipe_catalog_id": "fixed_l0_l3_v1",
        },
        "windows": {
            "research_train": {"start": "2020-08-03", "end": "2024-04-30"},
            "research_valid": {"start": "2024-05-01", "end": "2024-07-31"},
            "full_model": {"start": "2020-08-03", "end": "2024-07-31"},
            "final_holdout": {"start": "2024-08-01", "end": "2024-10-31"},
        },
        "views": {
            "stage1_view_id": "stage1_entry_view_v1",
            "stage2_view_id": "stage2_direction_view_v1",
            "stage3_view_id": "stage3_recipe_view_v1",
        },
        "labels": {
            "stage1_labeler_id": "entry_best_recipe_v1",
            "stage2_labeler_id": "direction_best_recipe_v1",
            "stage3_labeler_id": "recipe_best_positive_v1",
        },
        "training": {
            "stage1_trainer_id": "binary_catalog_v1",
            "stage2_trainer_id": "binary_catalog_v1",
            "stage3_trainer_id": "ovr_recipe_catalog_v1",
            "preprocess": preprocess.to_dict(),
            "cv_config": {
                "train_days": 84,
                "valid_days": 21,
                "test_days": 21,
                "step_days": 21,
                "purge_days": 0,
                "embargo_days": 0,
                "purge_mode": "days",
                "embargo_rows": 0,
                "event_end_col": None,
            },
            "objectives_by_stage": {
                "stage1": "brier",
                "stage2": "brier",
                "stage3": "brier",
            },
            "random_state": 42,
            "runtime": {"model_n_jobs": 4},
            "cost_per_trade": 0.0006,
        },
        "policy": {
            "stage1_policy_id": "entry_threshold_v1",
            "stage2_policy_id": "direction_dual_threshold_v1",
            "stage3_policy_id": "recipe_top_margin_v1",
            "stage1": {"threshold_grid": [0.45, 0.50, 0.55, 0.60]},
            "stage2": {
                "ce_threshold_grid": [0.55, 0.60, 0.65],
                "pe_threshold_grid": [0.55, 0.60, 0.65],
                "min_edge_grid": [0.05, 0.10, 0.15],
            },
            "stage3": {
                "threshold_grid": [0.45, 0.50, 0.55, 0.60],
                "margin_grid": [0.02, 0.05, 0.10],
            },
        },
        "runtime": {
            "prefilter_gate_ids": [
                "rollout_guard_v1",
                "risk_halt_pause_v1",
                "valid_entry_phase_v1",
                "startup_warmup_v1",
                "feature_freshness_v1",
                "feature_completeness_v1",
                "liquidity_gate_v1",
                "regime_gate_v1",
                "regime_confidence_gate_v1",
            ]
        },
        "publish": {"publisher_id": "staged_bundle_v1"},
        "hard_gates": {
            "stage1": {
                "roc_auc_min": 0.55,
                "brier_max": 0.22,
                "roc_auc_drift_half_split_max_abs": 0.05,
            },
            "stage2": {
                "roc_auc_min": 0.55,
                "brier_max": 0.22,
            },
            "stage3": {"max_drawdown_slack": 0.01},
            "combined": {
                "profit_factor_min": 1.50,
                "max_drawdown_pct_max": 0.10,
                "trades_min": 50,
                "net_return_sum_min": 0.0,
                "side_share_min": 0.30,
                "side_share_max": 0.70,
                "block_rate_min": 0.25,
            },
        },
    }

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from strategy_app.engines.ml_entry_policy import (
    ENTRY_MODEL_CATEGORICAL_COLUMNS,
    ENTRY_MODEL_FEATURE_COLUMNS,
    ENTRY_MODEL_NUMERIC_COLUMNS,
)

from .pipeline_layout import ARTIFACTS_ROOT, DATA_ROOT, PARQUET_DATA_ROOT


DEFAULT_CONTINUOUS_START = "2020-01-01"
DEFAULT_CONTINUOUS_END = "2021-10-14"
DEFAULT_TRAIN_START = "2020-01-01"
DEFAULT_TRAIN_END = "2020-12-31"
DEFAULT_VALID_START = "2021-01-01"
DEFAULT_VALID_END = "2021-06-30"
DEFAULT_EVAL_START = "2021-07-01"
DEFAULT_EVAL_END = "2021-10-14"

DEFAULT_ENTRY_QUALITY_ROOT = DATA_ROOT / "entry_quality"
DEFAULT_EDA_ROOT = DEFAULT_ENTRY_QUALITY_ROOT / "eda"
DEFAULT_CANDIDATE_ROOT = DEFAULT_ENTRY_QUALITY_ROOT / "candidates" / "v1"
DEFAULT_MODEL_ROOT = ARTIFACTS_ROOT / "models" / "entry_quality"

PRIMARY_LABEL_MFE_15_GT_5 = "mfe15_gt_5_v1"
LABEL_MFE_15_GT_8 = "mfe15_gt_8_v1"
LABEL_MFE_10_GT_5 = "mfe10_gt_5_v1"
LABEL_PNL_GT_5 = "pnl_gt_5_v1"
LABEL_CAPTURE_GT_40 = "capture_gt_40_v1"

SEG_GLOBAL = "seg_global_v1"
SEG_REGIME = "seg_regime_v1"
SEG_REGIME_NO_SIDEWAYS = "seg_regime_no_sideways_v1"

THRESHOLD_FIXED_060 = "fixed_060"
THRESHOLD_FIXED_065 = "fixed_065"
THRESHOLD_SEGMENT_OPTIMAL = "segment_optimal"
THRESHOLD_STRATEGY_OVERRIDE = "strategy_override_v1"

FEATURE_PROFILE_ALIASES: dict[str, str] = {
    "eq_core_snapshot_no_policy_v1": "eq_core_snapshot_v1",
    "eq_strategy_aware_v1": "eq_full_v1",
}


@dataclass(frozen=True)
class EntryQualitySplitConfig:
    train_start: str = DEFAULT_TRAIN_START
    train_end: str = DEFAULT_TRAIN_END
    valid_start: str = DEFAULT_VALID_START
    valid_end: str = DEFAULT_VALID_END
    eval_start: str = DEFAULT_EVAL_START
    eval_end: str = DEFAULT_EVAL_END


@dataclass(frozen=True)
class EntryQualityDatasetConfig:
    snapshot_base: Path = PARQUET_DATA_ROOT
    start_date: str = DEFAULT_CONTINUOUS_START
    end_date: str = DEFAULT_CONTINUOUS_END
    output_root: Path = DEFAULT_CANDIDATE_ROOT
    primary_horizon_bars: int = 15
    primary_threshold_pct: float = 0.05
    split: EntryQualitySplitConfig = field(default_factory=EntryQualitySplitConfig)


@dataclass(frozen=True)
class EntryQualityFeatureProfile:
    profile_id: str
    description: str
    numeric_columns: tuple[str, ...]
    categorical_columns: tuple[str, ...]

    @property
    def feature_columns(self) -> list[str]:
        return list(self.numeric_columns) + list(self.categorical_columns)


@dataclass(frozen=True)
class EntryQualityLabelProfile:
    profile_id: str
    column_name: str
    description: str
    requires_executed_trade_alignment: bool = False


@dataclass(frozen=True)
class EntryQualitySegmentationPolicy:
    policy_id: str
    description: str
    segments: tuple[str, ...]
    use_global_model: bool = False


@dataclass(frozen=True)
class EntryQualityModelSpec:
    model_id: str
    family: str
    params: dict[str, Any]


@dataclass(frozen=True)
class EntryQualityThresholdPolicy:
    policy_id: str
    description: str
    default_threshold: float | None = None
    strategy_overrides: dict[str, float] = field(default_factory=dict)
    strategy_regime_overrides: dict[tuple[str, str], float] = field(default_factory=dict)


BASE_NUMERIC_COLUMNS: tuple[str, ...] = tuple(ENTRY_MODEL_NUMERIC_COLUMNS)
BASE_CATEGORICAL_COLUMNS: tuple[str, ...] = tuple(ENTRY_MODEL_CATEGORICAL_COLUMNS)
POLICY_DIAGNOSTIC_NUMERIC: tuple[str, ...] = ("policy_score",)
POLICY_DIAGNOSTIC_CATEGORICAL: tuple[str, ...] = (
    "policy_check_volume",
    "policy_check_momentum",
    "policy_check_timing",
    "policy_check_premium",
    "policy_check_regime",
)
OPTION_MICROSTRUCTURE_NUMERIC: tuple[str, ...] = (
    "iv_percentile",
    "iv_skew",
    "atm_ce_close",
    "atm_pe_close",
    "atm_ce_vol_ratio",
    "atm_pe_vol_ratio",
    "atm_ce_oi_change_30m",
    "atm_pe_oi_change_30m",
    "pcr",
    "pcr_change_30m",
)
STRUCTURE_MOMENTUM_NUMERIC: tuple[str, ...] = (
    "minutes_since_open",
    "days_to_expiry",
    "fut_return_5m",
    "fut_return_15m",
    "fut_return_30m",
    "vol_ratio",
    "realized_vol_30m",
    "price_vs_vwap",
    "or_width",
    "price_vs_orh",
    "price_vs_orl",
)


FEATURE_PROFILES: dict[str, EntryQualityFeatureProfile] = {
    "eq_core_snapshot_v1": EntryQualityFeatureProfile(
        profile_id="eq_core_snapshot_v1",
        description="Snapshot and vote core features with strategy/regime identity.",
        numeric_columns=tuple(
            col for col in BASE_NUMERIC_COLUMNS if col not in POLICY_DIAGNOSTIC_NUMERIC
        ),
        categorical_columns=("strategy_name", "direction", "regime"),
    ),
    "eq_option_microstructure_v1": EntryQualityFeatureProfile(
        profile_id="eq_option_microstructure_v1",
        description="Option-state heavy profile for premium, IV, and OI/volume.",
        numeric_columns=OPTION_MICROSTRUCTURE_NUMERIC + ("vote_confidence", "regime_confidence"),
        categorical_columns=("strategy_name", "direction", "regime"),
    ),
    "eq_structure_momentum_v1": EntryQualityFeatureProfile(
        profile_id="eq_structure_momentum_v1",
        description="Structure and momentum heavy profile.",
        numeric_columns=STRUCTURE_MOMENTUM_NUMERIC + ("vote_confidence", "regime_confidence"),
        categorical_columns=("strategy_name", "direction", "regime"),
    ),
    "eq_full_v1": EntryQualityFeatureProfile(
        profile_id="eq_full_v1",
        description="All currently supported entry-quality features.",
        numeric_columns=BASE_NUMERIC_COLUMNS,
        categorical_columns=BASE_CATEGORICAL_COLUMNS,
    ),
}

LABEL_PROFILES: dict[str, EntryQualityLabelProfile] = {
    PRIMARY_LABEL_MFE_15_GT_5: EntryQualityLabelProfile(
        profile_id=PRIMARY_LABEL_MFE_15_GT_5,
        column_name="label_primary_mfe_hit",
        description="Primary baseline label: future 15-bar MFE exceeds 5%.",
    ),
    LABEL_MFE_15_GT_8: EntryQualityLabelProfile(
        profile_id=LABEL_MFE_15_GT_8,
        column_name="label_mfe_gt_8pct_15bars",
        description="Future 15-bar MFE exceeds 8%.",
    ),
    LABEL_MFE_10_GT_5: EntryQualityLabelProfile(
        profile_id=LABEL_MFE_10_GT_5,
        column_name="label_mfe_gt_5pct_10bars",
        description="Future 10-bar MFE exceeds 5%.",
    ),
    LABEL_PNL_GT_5: EntryQualityLabelProfile(
        profile_id=LABEL_PNL_GT_5,
        column_name="label_pnl_gt_5",
        description="Executed-trade-aligned label: pnl_pct exceeds 5%.",
        requires_executed_trade_alignment=True,
    ),
    LABEL_CAPTURE_GT_40: EntryQualityLabelProfile(
        profile_id=LABEL_CAPTURE_GT_40,
        column_name="label_capture_gt_40",
        description="Executed-trade-aligned label: capture ratio exceeds 40%.",
        requires_executed_trade_alignment=True,
    ),
}

SEGMENTATION_POLICIES: dict[str, EntryQualitySegmentationPolicy] = {
    SEG_GLOBAL: EntryQualitySegmentationPolicy(
        policy_id=SEG_GLOBAL,
        description="Single model across all regimes.",
        segments=("GLOBAL",),
        use_global_model=True,
    ),
    SEG_REGIME: EntryQualitySegmentationPolicy(
        policy_id=SEG_REGIME,
        description="Separate models for TRENDING, PRE_EXPIRY, and SIDEWAYS.",
        segments=("TRENDING", "PRE_EXPIRY", "SIDEWAYS"),
    ),
    SEG_REGIME_NO_SIDEWAYS: EntryQualitySegmentationPolicy(
        policy_id=SEG_REGIME_NO_SIDEWAYS,
        description="Separate models for TRENDING and PRE_EXPIRY only.",
        segments=("TRENDING", "PRE_EXPIRY"),
    ),
}

MODEL_SPECS: dict[str, EntryQualityModelSpec] = {
    "logreg_baseline_v1": EntryQualityModelSpec(
        model_id="logreg_baseline_v1",
        family="logreg",
        params={"max_iter": 1000, "solver": "lbfgs"},
    ),
    "lgbm_default_v1": EntryQualityModelSpec(
        model_id="lgbm_default_v1",
        family="lgbm",
        params={"n_estimators": 400, "learning_rate": 0.05, "num_leaves": 31, "subsample": 0.9, "colsample_bytree": 0.9},
    ),
    "lgbm_shallow_v1": EntryQualityModelSpec(
        model_id="lgbm_shallow_v1",
        family="lgbm",
        params={"n_estimators": 300, "learning_rate": 0.05, "num_leaves": 15, "max_depth": 4, "subsample": 0.9, "colsample_bytree": 0.9},
    ),
    "lgbm_regularized_v1": EntryQualityModelSpec(
        model_id="lgbm_regularized_v1",
        family="lgbm",
        params={"n_estimators": 350, "learning_rate": 0.04, "num_leaves": 21, "min_child_samples": 40, "reg_alpha": 0.2, "reg_lambda": 0.5, "subsample": 0.85, "colsample_bytree": 0.85},
    ),
    "xgb_default_v1": EntryQualityModelSpec(
        model_id="xgb_default_v1",
        family="xgb",
        params={"n_estimators": 350, "learning_rate": 0.05, "max_depth": 4, "subsample": 0.9, "colsample_bytree": 0.9, "reg_lambda": 1.0},
    ),
}

THRESHOLD_POLICIES: dict[str, EntryQualityThresholdPolicy] = {
    THRESHOLD_FIXED_060: EntryQualityThresholdPolicy(
        policy_id=THRESHOLD_FIXED_060,
        description="Fixed 0.60 threshold across all segments and strategies.",
        default_threshold=0.60,
    ),
    THRESHOLD_FIXED_065: EntryQualityThresholdPolicy(
        policy_id=THRESHOLD_FIXED_065,
        description="Fixed 0.65 threshold across all segments and strategies.",
        default_threshold=0.65,
    ),
    THRESHOLD_SEGMENT_OPTIMAL: EntryQualityThresholdPolicy(
        policy_id=THRESHOLD_SEGMENT_OPTIMAL,
        description="Use per-segment optimal threshold selected from validation sweep.",
        default_threshold=None,
    ),
    THRESHOLD_STRATEGY_OVERRIDE: EntryQualityThresholdPolicy(
        policy_id=THRESHOLD_STRATEGY_OVERRIDE,
        description="Use strategy-aware threshold overrides layered over segment defaults.",
        default_threshold=None,
        strategy_overrides={
            "OI_BUILDUP": 0.62,
            "ORB": 0.66,
            "EMA_CROSSOVER": 0.80,
            "VWAP_RECLAIM": 0.62,
            "PREV_DAY_LEVEL": 0.67,
        },
        strategy_regime_overrides={
            ("SIDEWAYS", "OI_BUILDUP"): 0.70,
            ("TRENDING", "OI_BUILDUP"): 0.64,
            ("PRE_EXPIRY", "OI_BUILDUP"): 0.66,
        },
    ),
}


def candidate_feature_columns_for_profile(profile_id: str) -> list[str]:
    profile = FEATURE_PROFILES[canonical_feature_profile_id(profile_id)]
    return profile.feature_columns


def canonical_feature_profile_id(profile_id: str) -> str:
    return FEATURE_PROFILE_ALIASES.get(profile_id, profile_id)


def normalize_feature_profile_ids(profile_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for profile_id in profile_ids:
        canonical = canonical_feature_profile_id(profile_id)
        if canonical in seen:
            continue
        if canonical not in FEATURE_PROFILES:
            raise KeyError(f"unknown feature profile: {profile_id}")
        normalized.append(canonical)
        seen.add(canonical)
    return normalized


def threshold_policy_for_id(policy_id: str) -> EntryQualityThresholdPolicy:
    return THRESHOLD_POLICIES[policy_id]


def split_name_for_trade_date(trade_date: str, split: EntryQualitySplitConfig | None = None) -> str:
    cfg = split or EntryQualitySplitConfig()
    text = str(trade_date)
    if cfg.train_start <= text <= cfg.train_end:
        return "train"
    if cfg.valid_start <= text <= cfg.valid_end:
        return "valid"
    if cfg.eval_start <= text <= cfg.eval_end:
        return "eval"
    return "out_of_window"

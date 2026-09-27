"""Entry-quality-conditioned direction research: data assembly + modeling
utilities (NIFTY, 2026-09-27).

This module answers a narrower question than this project's six prior,
exhausted direction-prediction attempts (see
``ml_pipeline_2.pipeline.conditional_direction``'s module docstring for the
full list -- momentum accuracy, the 31-combination ablation grid, the 2026
OOS quorum inversion, order-flow proxies, high-ML-probability direction
splits, and 125 live trades, all converging near a 50-56% coin flip): is
CE-vs-PE direction more callable specifically on the bars an INDEPENDENT
entry-timing model (``ml_pipeline_2.scripts.train_entry_option_payoff_v1``,
which predicts ``max(net CE return, net PE return)`` -- itself
direction-agnostic) rates as high quality?

Import-isolation constraint (matches ``conditional_direction.py`` and the
owning study script's own docstring): this module must NEVER import
``strategy_app.ml.entry_direction_resolver``, ``strategy_app.engines
.strategies.ml_entry``, or anything under those packages -- this is
registry-recorded research, not a change to the live serving path. It DOES
import ``strategy_app.market.snapshot_accessor.SnapshotAccessor`` and
``strategy_app.cost_model.TradingCostModel`` -- plain data/cost utilities,
not the resolver -- exactly as ``ml_pipeline_2.pipeline.option_payoff_labels``
already does.

Three families of function live here:

1. Raw-signal extraction (``build_raw_signal_frame``) -- pulls the exact raw
   snapshot fields ``conditional_direction.SIGNAL_VOTERS`` needs (momentum,
   VWAP side, VIX change, IV skew, opening-range state, PCR change) straight
   off real ``SnapshotAccessor`` objects. This exists because the NIFTY
   training-view CSV (``training_view_nifty_option_pnl.csv.gz``) carries
   ``ENTRY_FEATURES_V3`` (velocity/context/EMA features) but NOT these raw
   resolver inputs (confirmed 2026-09-27 by inspecting its real header) --
   they have to be re-read from the raw snapshot parquet and joined on.
2. Entry-payoff proxy fitting (``fit_entry_payoff_model`` /
   ``fit_entry_payoff_oof_scores`` / ``score_entry_payoff``) -- same
   architecture as ``train_entry_option_payoff_v1.py`` (XGBoost regressor,
   winsorized target), reused here as a plain fit/score utility so the
   walk-forward study script can retrain it per rolling window instead of
   depending on one fixed train/valid/holdout split.
3. Direction classifier fitting (``fit_direction_classifier`` /
   ``predict_direction_side`` / ``encode_side_binary``) -- a small XGBoost
   classifier predicting ``payoff_winning_side`` (CE=1/PE=0) directly, used
   to compare a model trained on ALL bars against one trained only on
   entry-quality top-decile bars (see
   ``ml_pipeline_2.scripts.study_entry_quality_conditioned_direction``).
4. ``compute_payoff_at_delay`` -- the delayed-action stress-test primitive
   (analogous to ``ml_pipeline_2.scripts.run_repricing_wall_control
   .reprice_fires_at_delays`` and ``validation.check_repricing_wall``):
   re-scores one fire bar's payoff using the entry premium sampled
   ``delay_bars`` bars AFTER the fire, holding to the SAME original target
   exit bar. Deliberately reimplemented here (not imported from
   ``ml_pipeline_2.pipeline.direction_replay``) because that module imports
   ``strategy_app.ml.entry_direction_resolver`` at module load time -- this
   study must not carry that import transitively, even unused.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from strategy_app.cost_model import TradingCostModel
from strategy_app.market.snapshot_accessor import SnapshotAccessor

from .option_payoff_labels import BarPayoff, compute_bar_payoff

# ── Raw resolver-input fields genuinely absent from the NIFTY training view ──
# `conditional_direction.REQUIRED_RAW_COLUMNS` lists all 12 raw fields the
# vote_* functions need. Of those, `fut_return_5m`/`fut_return_15m`/
# `fut_close`/`vix_intraday_chg` ALREADY exist, unsuffixed, in
# `training_view_nifty_option_pnl.csv.gz` (confirmed 2026-09-27: numerically
# IDENTICAL to a fresh SnapshotAccessor read of the same bars, down to float
# noise -- same feature engine, computed once). Extracting them again here
# and joining would collide on those column names and get silently
# pandas-suffixed (`_x`/`_y`) instead of raising, which is exactly what
# happened on the first real run of this module (both suffixed copies were
# 100% populated -- the resulting "0% coverage" this module's own logging
# reported was a bug in that logging, not a real data gap). So only the
# columns NOT already in the training view are extracted here.
RAW_SIGNAL_COLUMNS: List[str] = [
    "price_vs_vwap", "atm_ce_iv", "atm_pe_iv", "orh", "orl", "orh_broken", "orl_broken",
    "pcr_change_5m",
]

# ~1-minute snapshot cadence (see option_payoff_labels.label_day's docstring)
# -- a 5-bar lookback approximates the resolver's "5m" pcr-change window.
PCR_CHANGE_LOOKBACK_BARS = 5


def extract_raw_signal_row(snap: SnapshotAccessor, *, bar_index: int) -> Dict[str, Any]:
    """One row of raw fields plus join/identity keys for a single snapshot.
    ``timestamp``/``trade_date`` are shaped exactly like
    ``build_option_pnl_training_view``'s label-frame rows so this can be fed
    straight into that module's ``join_features_to_labels`` (reused, not
    re-derived, for the join itself). Carries the raw `pcr` LEVEL (not yet
    `pcr_change_5m` -- see `build_raw_signal_frame`'s derivation, which needs
    the whole day's chronological sequence, not one bar in isolation)."""
    return {
        "trade_date": snap.trade_date,
        "timestamp": snap.raw_payload.get("timestamp"),
        "bar_index": bar_index,
        "price_vs_vwap": snap.price_vs_vwap,
        "atm_ce_iv": snap.atm_ce_iv,
        "atm_pe_iv": snap.atm_pe_iv,
        "orh": snap.orh,
        "orl": snap.orl,
        "orh_broken": snap.orh_broken,
        "orl_broken": snap.orl_broken,
        "pcr": snap.pcr,
    }


def derive_pcr_change(pcr: pd.Series, *, lookback: int = PCR_CHANGE_LOOKBACK_BARS) -> pd.Series:
    """`pcr_change_5m` proxy: `pcr[t] - pcr[t-lookback]` within ONE
    chronological day's bars. The archive's own precomputed
    `chain_aggregates.pcr_change_5m` field is ALWAYS null across the FULL
    2024-11..2026-07 NIFTY history (confirmed 2026-09-27 by direct
    inspection of multiple dates spanning the whole range -- a real,
    project-wide gap in this historical archive, not a per-day artifact or
    a live-vs-historical schema difference), so this study derives it from
    the raw `pcr` level (which IS fully populated) instead of reading it
    directly, unlike every other raw signal column here. Callers must pass
    one day's `pcr` values in chronological order -- this function does not
    itself guard against crossing a trade_date boundary."""
    return pd.to_numeric(pcr, errors="coerce").diff(lookback)


def build_raw_signal_frame(snaps: Sequence[SnapshotAccessor]) -> pd.DataFrame:
    """Raw-signal frame for one chronological day of snapshots. ``bar_index``
    is the 0-based position within `snaps` (assumed already sorted, matching
    ``read_day_snapshots``' own sort-by-timestamp contract) -- this is what
    lets the delayed-action stress test later re-locate the exact same bar
    inside a freshly re-read day's snapshot list."""
    cols = ["trade_date", "timestamp", "bar_index"] + RAW_SIGNAL_COLUMNS
    if not snaps:
        return pd.DataFrame(columns=cols)
    rows = [extract_raw_signal_row(s, bar_index=i) for i, s in enumerate(snaps)]
    df = pd.DataFrame(rows)
    df["pcr_change_5m"] = derive_pcr_change(df["pcr"])
    return df[cols]


def compute_payoff_at_delay(
    snaps: Sequence[SnapshotAccessor],
    fire_idx: int,
    *,
    horizon_bars: int,
    delay_bars: int,
    lot_size: int,
    cost_model: Optional[TradingCostModel] = None,
) -> Optional[BarPayoff]:
    """Re-score `fire_idx`'s payoff using the entry premium sampled
    `delay_bars` bars AFTER the fire, holding to the SAME original target
    exit bar (`fire_idx + horizon_bars`, fixed) -- the direction-agnostic
    payoff analog of `validation.check_repricing_wall`'s own re-pricing
    pattern (see this module's docstring for why this is a deliberate,
    independent reimplementation rather than an import of
    `direction_replay.compute_bar_payoff_at_delay`). Returns None if either
    the delayed entry bar or the (unchanged) exit bar falls outside the
    day's session, or if the delayed entry would be AFTER the exit."""
    exit_idx = fire_idx + horizon_bars
    entry_idx = fire_idx + delay_bars
    if exit_idx >= len(snaps) or entry_idx >= len(snaps) or entry_idx > exit_idx:
        return None
    cm = cost_model or TradingCostModel()
    return compute_bar_payoff(snaps[entry_idx], snaps[exit_idx], lot_size=lot_size, cost_model=cm)


def encode_side_binary(side: pd.Series) -> np.ndarray:
    """CE -> 1, PE -> 0. Raises on any other value -- every row this study
    includes already has a resolved `payoff_winning_side` (option_payoff_labels
    never emits a third value), so silently coercing anything else to NaN
    would hide a real upstream bug rather than surface it."""
    values = side.astype(str)
    unknown = set(values.unique()) - {"CE", "PE"}
    if unknown:
        raise ValueError(f"encode_side_binary: unexpected payoff_winning_side values {sorted(unknown)}")
    return (values == "CE").astype(int).to_numpy()


# ── Entry-payoff proxy (same architecture as train_entry_option_payoff_v1) ──

DEFAULT_XGB_REGRESSOR_PARAMS: Dict[str, Any] = dict(
    n_estimators=300, max_depth=4, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
    objective="reg:squarederror", random_state=42, n_jobs=-1,
)

DEFAULT_XGB_CLASSIFIER_PARAMS: Dict[str, Any] = dict(
    n_estimators=200, max_depth=4, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8,
    eval_metric="logloss", random_state=42, n_jobs=-1,
)


def _prep_features(df: pd.DataFrame, feature_cols: Sequence[str], medians: Optional[pd.Series] = None) -> Tuple[pd.DataFrame, pd.Series]:
    X = df[list(feature_cols)].apply(pd.to_numeric, errors="coerce")
    meds = medians if medians is not None else X.median()
    return X.fillna(meds).fillna(0.0), meds


def winsorize_target(y: np.ndarray, *, lower_pct: float = 0.01, upper_pct: float = 0.99) -> Tuple[np.ndarray, float, float]:
    """Clip extreme tail values in the TRAINING target before fitting a
    squared-error regressor. Deliberately duplicated from
    `ml_pipeline_2.scripts.train_entry_option_payoff_v1.winsorize_target`
    (identical logic, same real-data motivation -- payoff_score's long
    right tail from near-worthless cheap entries) rather than imported: a
    `pipeline/` module importing from `scripts/` would invert this
    package's layering (scripts depend on pipeline, never the reverse) and
    would also trip `test_boundaries.py`'s substring guard against
    `ml_pipeline_2/src/ml_pipeline_2/**` files spelling out a fully-qualified
    `ml_pipeline_2....` import (that guard exists to keep this package from
    ever importing the legacy, pre-`_2` `ml_pipeline` package; it can't
    distinguish that from a self-referential fully-qualified import of
    `ml_pipeline_2` itself, so this module uses relative imports for its
    one genuine intra-pipeline dependency and this small duplication for
    its one genuine cross-layer one)."""
    lo = float(np.quantile(y, lower_pct))
    hi = float(np.quantile(y, upper_pct))
    return np.clip(y, lo, hi), lo, hi


def fit_entry_payoff_model(
    train_df: pd.DataFrame,
    feature_cols: Sequence[str],
    target_col: str,
    *,
    xgb_params: Optional[Dict[str, Any]] = None,
    winsorize_lower_pct: float = 0.01,
    winsorize_upper_pct: float = 0.99,
) -> Tuple[Any, pd.Series]:
    """Fit an XGBRegressor predicting `target_col` (meant to be
    `payoff_score`) on `train_df[feature_cols]`, winsorizing the training
    target first (see this module's own `winsorize_target` -- same
    discipline as `train_entry_option_payoff_v1.py`'s function of the same
    name). Returns (model, feature_medians) for later NaN-fill-consistent
    scoring via `score_entry_payoff`."""
    import xgboost as xgb

    X, medians = _prep_features(train_df, feature_cols)
    y = pd.to_numeric(train_df[target_col], errors="coerce").to_numpy()
    y_train, _, _ = winsorize_target(y, lower_pct=winsorize_lower_pct, upper_pct=winsorize_upper_pct)
    params = dict(xgb_params or DEFAULT_XGB_REGRESSOR_PARAMS)
    model = xgb.XGBRegressor(**params)
    model.fit(X, y_train)
    return model, medians


def score_entry_payoff(model: Any, medians: pd.Series, df: pd.DataFrame, feature_cols: Sequence[str]) -> np.ndarray:
    X, _ = _prep_features(df, feature_cols, medians=medians)
    return np.asarray(model.predict(X), dtype=float)


def fit_entry_payoff_oof_scores(
    train_df: pd.DataFrame,
    feature_cols: Sequence[str],
    target_col: str,
    *,
    n_splits: int = 5,
    random_state: int = 42,
    xgb_params: Optional[Dict[str, Any]] = None,
    winsorize_lower_pct: float = 0.01,
    winsorize_upper_pct: float = 0.99,
    min_rows_to_fit: int = 50,
) -> np.ndarray:
    """K-fold out-of-fold entry-payoff scores for TRAIN rows only -- no row
    is ever scored by a model that saw its own label. This is what lets the
    walk-forward study decide which TRAIN bars are "top entry-quality
    decile" (needed to build the entry-quality-CONDITIONED direction
    classifier's training set) without leaking each row's own target into
    its own decile assignment. Falls back to a constant (the raw target's
    median) when there isn't enough data to fit even one fold -- callers
    should treat a study window this thin as informational only (mirrors
    `train_direction_meta_v1.fit_entry_quality_scores`'s own fallback)."""
    import xgboost as xgb
    from sklearn.model_selection import KFold

    X, medians = _prep_features(train_df, feature_cols)
    y = pd.to_numeric(train_df[target_col], errors="coerce").to_numpy()
    n = len(train_df)
    fallback = float(np.nanmedian(y)) if n else 0.0
    oof = np.full(n, fallback)
    if n < max(min_rows_to_fit, n_splits * 5):
        return oof

    params = dict(xgb_params or DEFAULT_XGB_REGRESSOR_PARAMS)
    kf = KFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    for tr_idx, va_idx in kf.split(X):
        y_tr, _, _ = winsorize_target(y[tr_idx], lower_pct=winsorize_lower_pct, upper_pct=winsorize_upper_pct)
        model = xgb.XGBRegressor(**params)
        model.fit(X.iloc[tr_idx], y_tr)
        oof[va_idx] = model.predict(X.iloc[va_idx])
    return oof


# ── Direction classifier (fresh, entry-quality-conditioned vs unconditioned) ─

def fit_direction_classifier(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    *,
    xgb_params: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, pd.Series]:
    """Fit an XGBClassifier predicting CE(1)/PE(0). Pure given already-sliced
    (X_train, y_train) -- the caller decides whether that slice is "all
    bars" or "top-decile bars only"."""
    import xgboost as xgb

    medians = X_train.median()
    Xf = X_train.fillna(medians).fillna(0.0)
    params = dict(xgb_params or DEFAULT_XGB_CLASSIFIER_PARAMS)
    model = xgb.XGBClassifier(**params)
    model.fit(Xf, y_train)
    return model, medians


def predict_direction_side(model: Any, medians: pd.Series, X: pd.DataFrame) -> np.ndarray:
    Xf = X.fillna(medians).fillna(0.0)
    return np.asarray(model.predict(Xf)).astype(int)


__all__ = [
    "RAW_SIGNAL_COLUMNS",
    "extract_raw_signal_row",
    "build_raw_signal_frame",
    "compute_payoff_at_delay",
    "encode_side_binary",
    "DEFAULT_XGB_REGRESSOR_PARAMS",
    "DEFAULT_XGB_CLASSIFIER_PARAMS",
    "fit_entry_payoff_model",
    "score_entry_payoff",
    "fit_entry_payoff_oof_scores",
    "fit_direction_classifier",
    "predict_direction_side",
]

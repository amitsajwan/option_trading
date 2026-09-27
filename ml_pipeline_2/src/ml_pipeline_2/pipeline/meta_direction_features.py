"""Feature engineering for the NIFTY direction-CALL-TRUST meta-labeling layer
(2026-09-26).

Background: this project has exhaustively tested (at least 6 independent
methodologies, see the 2026-09 direction-forensics memory trail) whether
ANY direction signal -- including ``strategy_app.ml.entry_direction_resolver.
resolve_entry_direction_composite`` (composite) itself and every one of its
individual weighted components -- beats a coin flip UNCONDITIONALLY. It does
not (~48-56% per-member accuracy). This module does NOT re-attempt that
question. It builds features for the different, narrower question: **can we
predict, bar by bar, whether composite's call is likely to be right or wrong
THIS TIME**, using information composite's own scoring function does not use?
This is meta-labeling applied to a direction-caller instead of an entry-timer
-- the exact same idea this codebase already shipped for entry quality in
``meta_entry_features.py`` (read that module's docstring first; this one
mirrors its design pattern and reasoning throughout).

Critical design constraint -- read before adding a feature here
=================================================================
If the meta-model's features are just a relabeling of whatever composite
already scores on (momentum_5m/15m, VWAP side, VIX intraday change, IV
skew, opening-range false-break rejection, PCR 5m change, order-book depth,
the minor direction-ML tilt), this experiment would just re-derive a
recalibration of composite's own confidence under a new name -- not new
information, and it would silently look like it "worked" in a backtest for
the wrong reason (composite's own margin already correlates with its own
accuracy at a trivial level: bigger margin bars have, by construction, more
signals agreeing, which is not the same as an independently verified trust
signal). The features below are restricted to THREE kinds of information
composite's scoring function, evaluated bar-by-bar in isolation, cannot
express:

1. RATE / ACCELERATION features -- how fast the option chain is currently
   repricing, not where it currently sits. Reused verbatim (not
   re-derived) from ``meta_entry_features.SELECTED_VELOCITY_FEATURES`` --
   the same "Layer 4 velocity" columns already selected and justified there
   (``snapshot_app/core/velocity_features.py``). Composite never reads any
   ``vel_*`` column.
2. REGIME / COMPRESSION-STATE features -- is the market currently
   consolidating or trending, and how strongly, independent of WHICH way it
   last moved. Selected from ``snapshot_app.core.compression_features.
   COMPRESSION_FEATURE_COLUMNS`` (the same Big-Move-Model compression block
   already computed per-bar into every snapshot's ``futures_derived``).
   Deliberately EXCLUDES that module's own directional-sign features
   (``ema_order``, ``ema_spread_9_21``, ``ema_spread_21_50``,
   ``dist_from_ema21``) -- those are trend-DIRECTION proxies uncomfortably
   close in spirit to composite's own momentum_5m/15m inputs (both ask
   "which way is price leaning right now"), so keeping them would blur the
   "genuinely new information" line this module exists to enforce. What
   remains (``bb_width_20``, ``bb_width_chg_5``, ``range_10``, ``range_30``,
   ``range_ratio_10_30``, ``candle_overlap_10``, ``position_in_day_range``,
   ``compression_score``, ``adx_14``, ``vol_spike_ratio``) characterizes
   volatility/consolidation REGIME state, not directional lean.
3. ``bars_since_composite_active_streak`` -- how long the CURRENT streak of
   composite NOT-abstaining (margin >= ENTRY_DIR_MIN_MARGIN) has been
   running. Reuses ``meta_entry_features.bars_since_primary_first_eligible``
   verbatim (a pure function of any boolean streak, already generic) with
   "eligible" redefined as "composite did not veto this bar". A fresh call
   right after a long silent stretch and the 10th call of an already-active
   run look identical to composite itself; they may not to a meta-model
   with this feature.

DELIBERATE EXCEPTION to (1)/(2)/(3)'s "not already an input" rule --
`ENTRY_QUALITY_SCORE_COL`: the (out-of-fold) predicted score of a SEPARATE
entry-quality/payoff model, trained on the different question "would ANY
option trade have cleared cost here" (magnitude/direction-agnostic), not
composite's question of "which side wins". This is a genuinely different
model answering a genuinely different question, so -- unlike
``meta_entry_features``'s own deliberate exclusion of its primary's raw
probability (where including it WOULD have just re-derived a recalibration
of the same model being evaluated) -- there's no re-derivation risk in
including it here at full (continuous) precision. The caller (``ml_pipeline_2
.scripts.build_direction_meta_dataset`` / ``train_direction_meta_v1``) is
responsible for computing this score walk-forward-safely (out-of-fold on
train, held-out on test) before handing it to `build_meta_direction_features`
-- this module treats it as a plain input float, same convention as
`primary_probability` in `meta_entry_features.build_meta_features`.

``test_meta_direction_features.py`` asserts none of composite's own raw
scoring inputs (fut_return_5m/15m, price_vs_vwap, vix_intraday_chg,
atm_ce_iv/pe_iv, pcr_change_5m, orh_broken/orl_broken, ce_score/pe_score/
margin) leak into `META_DIRECTION_FEATURE_COLUMNS`.
"""
from __future__ import annotations

import math
from typing import Dict, List, Mapping, Sequence

import numpy as np
import pandas as pd

from snapshot_app.core.compression_features import COMPRESSION_FEATURE_COLUMNS
from .meta_entry_features import (
    SELECTED_VELOCITY_FEATURES,
    bars_since_primary_first_eligible,
    select_velocity_rate_features,
    select_velocity_rate_features_df,
)

# ── Selected regime/compression-state columns (see module docstring #2) ────
# Deliberately excludes ema_order/ema_spread_9_21/ema_spread_21_50/
# dist_from_ema21 -- directional-lean proxies too close to composite's own
# momentum_5m/15m inputs. Already computed per-bar by the live snapshot
# pipeline into futures_derived -- read straight off the payload, never
# recomputed here (see `select_regime_features`).
SELECTED_REGIME_FEATURES: List[str] = [
    "bb_width_20",
    "bb_width_chg_5",
    "range_10",
    "range_30",
    "range_ratio_10_30",
    "candle_overlap_10",
    "position_in_day_range",
    "compression_score",
    "adx_14",
    "vol_spike_ratio",
]

_unknown_regime = [c for c in SELECTED_REGIME_FEATURES if c not in COMPRESSION_FEATURE_COLUMNS]
if _unknown_regime:
    raise RuntimeError(
        "meta_direction_features.SELECTED_REGIME_FEATURES references column(s) no longer produced "
        f"by snapshot_app.core.compression_features.COMPRESSION_FEATURE_COLUMNS: {_unknown_regime}. "
        "The source module's column names changed -- go re-read it and update the selection above "
        "rather than let this silently drift (do not guess names)."
    )

# ── New engineered feature: composite-active streak length ─────────────────
BARS_SINCE_ACTIVE_COL = "bars_since_composite_active_streak"

# ── Entry-quality model's own predicted score (continuous -- see module
#    docstring's "deliberate exception") ────────────────────────────────────
ENTRY_QUALITY_SCORE_COL = "entry_quality_score"

# Final, ordered column list the direction-trust meta-model is trained on.
META_DIRECTION_FEATURE_COLUMNS: List[str] = (
    list(SELECTED_VELOCITY_FEATURES)
    + list(SELECTED_REGIME_FEATURES)
    + [BARS_SINCE_ACTIVE_COL, ENTRY_QUALITY_SCORE_COL]
)

# Composite's own raw scoring inputs -- must NEVER appear in
# META_DIRECTION_FEATURE_COLUMNS (enforced by a unit test, not just this
# comment). Kept here as a single source of truth for that test.
COMPOSITE_OWN_INPUT_NAMES: frozenset[str] = frozenset(
    {
        "fut_return_5m", "fut_return_15m", "price_vs_vwap", "vix_intraday_chg",
        "atm_ce_iv", "atm_pe_iv", "pcr_change_5m", "orh_broken", "orl_broken",
        "orh", "orl", "ce_score", "pe_score", "margin", "depth_net",
    }
)


def bars_since_composite_active(fired: Sequence[bool]) -> list[int]:
    """Thin, semantically-named wrapper over
    `meta_entry_features.bars_since_primary_first_eligible` -- "eligible"
    redefined as "composite did not veto this bar" (see module docstring
    #3). `fired` must already be in chronological order over the FULL day's
    bars, including bars where composite abstained."""
    return bars_since_primary_first_eligible(fired)


def select_regime_features(futures_derived: Mapping[str, float]) -> Dict[str, float]:
    """Pull SELECTED_REGIME_FEATURES out of one bar's already-computed
    ``futures_derived`` block (e.g. ``SnapshotAccessor.raw_payload
    ["futures_derived"]``) -- these are already computed by the live
    snapshot pipeline (``snapshot_app.core.compression_features
    .add_compression_features``), never recomputed here. Missing columns
    (upstream schema drift, or an older snapshot predating this feature
    block) degrade to NaN rather than raising, matching
    `meta_entry_features.select_velocity_rate_features`'s convention."""
    out: Dict[str, float] = {}
    for col in SELECTED_REGIME_FEATURES:
        val = futures_derived.get(col) if hasattr(futures_derived, "get") else None
        try:
            out[col] = (
                float(val)
                if val is not None and not (isinstance(val, float) and math.isnan(val))
                else float("nan")
            )
        except (TypeError, ValueError):
            out[col] = float("nan")
    return out


def select_regime_features_df(df: pd.DataFrame) -> pd.DataFrame:
    """DataFrame counterpart of `select_regime_features` -- `df` already has
    SELECTED_REGIME_FEATURES as plain columns (e.g. built by flattening each
    bar's `futures_derived` block). Missing columns become an all-NaN
    column (with no raise), matching
    `meta_entry_features.select_velocity_rate_features_df`'s convention."""
    out = pd.DataFrame(index=df.index)
    for col in SELECTED_REGIME_FEATURES:
        if col in df.columns:
            out[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            out[col] = np.nan
    return out


def build_meta_direction_features(
    velocity_row: Mapping[str, float],
    futures_derived: Mapping[str, float],
    *,
    bars_since_active: int,
    entry_quality_score: float,
) -> Dict[str, float]:
    """Assemble the final meta-model feature row for ONE composite call
    (fired, non-abstained bar): velocity rate/acceleration features +
    regime/compression features + the active-streak length +  the
    (walk-forward-safe, already-scored) entry-quality score.

    Returns exactly the keys in META_DIRECTION_FEATURE_COLUMNS."""
    feats = select_velocity_rate_features(velocity_row)
    feats.update(select_regime_features(futures_derived))
    feats[BARS_SINCE_ACTIVE_COL] = float(bars_since_active)
    feats[ENTRY_QUALITY_SCORE_COL] = (
        float(entry_quality_score) if entry_quality_score is not None and math.isfinite(float(entry_quality_score))
        else float("nan")
    )
    return feats


def build_meta_direction_features_df(
    velocity_df: pd.DataFrame,
    regime_df: pd.DataFrame,
    *,
    bars_since_active: Sequence[int],
    entry_quality_score: Sequence[float],
) -> pd.DataFrame:
    """DataFrame counterpart of `build_meta_direction_features`.
    `velocity_df`, `regime_df`, `bars_since_active`, and
    `entry_quality_score` must all be pre-aligned, same-length sequences
    (positionally, not by index)."""
    n = len(velocity_df)
    if len(regime_df) != n or len(bars_since_active) != n or len(entry_quality_score) != n:
        raise ValueError(
            "build_meta_direction_features_df: velocity_df, regime_df, bars_since_active and "
            f"entry_quality_score must all be the same length (got {n}, {len(regime_df)}, "
            f"{len(bars_since_active)}, {len(entry_quality_score)})"
        )
    feats = select_velocity_rate_features_df(velocity_df).reset_index(drop=True)
    regime_feats = select_regime_features_df(regime_df).reset_index(drop=True)
    for col in SELECTED_REGIME_FEATURES:
        feats[col] = regime_feats[col]
    feats[BARS_SINCE_ACTIVE_COL] = [float(b) for b in bars_since_active]
    feats[ENTRY_QUALITY_SCORE_COL] = [
        float(s) if s is not None and math.isfinite(float(s)) else float("nan") for s in entry_quality_score
    ]
    return feats[META_DIRECTION_FEATURE_COLUMNS]


__all__ = [
    "SELECTED_VELOCITY_FEATURES",
    "SELECTED_REGIME_FEATURES",
    "BARS_SINCE_ACTIVE_COL",
    "ENTRY_QUALITY_SCORE_COL",
    "META_DIRECTION_FEATURE_COLUMNS",
    "COMPOSITE_OWN_INPUT_NAMES",
    "bars_since_composite_active",
    "select_regime_features",
    "select_regime_features_df",
    "build_meta_direction_features",
    "build_meta_direction_features_df",
]

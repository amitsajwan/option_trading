"""Feature engineering for the NIFTY entry-timing META-LABELING layer
(2026-09-26 focused rebuild -- direction-calling deliberately deferred).

Background (see the task/registry notes this module was built against):
three independent prior attempts (``docs/archive/entry_model/
MODEL_REBUILD_SPEC_2026-07-11.md`` Amendment 3/4 and ``docs/archive/
entry_model/ENTRY_MODEL_RARELABEL_REBUILD_2026-07-12.md``) all found that
buying options the moment a movement-detection ("primary") model becomes
confident loses money at every horizon -- the option chain has already
repriced by the moment of confident detection. All three tests traded
EVERY fire unconditionally. This module builds the FEATURES for a
meta-model that instead asks whether a SUBSET of fires escape that wall --
trained only on bars where the primary fired, targeting whether that
specific fire's cost-aware payoff (see
``ml_pipeline_2.pipeline.option_payoff_labels``) cleared zero.

Critical design constraint -- read before adding a feature here
=================================================================
If the meta-model's features are just a relabeling of whatever the primary
model already uses (price/EMA/momentum-style LEVEL features), this
experiment re-derives the exact same wall under a new name. The features
below are deliberately restricted to two kinds of information the primary
model, scored bar-by-bar in isolation, cannot express:

1. RATE / ACCELERATION features -- how fast the option chain is currently
   repricing, not where it currently sits. These are selected (not
   recomputed -- see ``SELECTED_VELOCITY_FEATURES`` below) from the
   existing "Layer 4 velocity" pipeline in
   ``snapshot_app/core/velocity_features.py``, which is already computed
   for both training and live serving.
2. ``bars_since_primary_first_eligible`` -- how long the CURRENT streak of
   primary-eligible bars has been running before this particular fire.
   A primary model scored one bar at a time has no memory of this; a fresh
   signal and the 5th bar of an already-stale elevated-probability streak
   look identical to it, but may not to a meta-model with this feature.

DELIBERATELY EXCLUDED: the primary model's raw, continuous fire-time
probability. Including it risks the meta-model just learning a monotonic
recalibration of the primary (e.g. "predict payoff>0 whenever
primary_probability > 0.91") instead of a genuinely different signal --
which would silently pass a meta-labeling backtest while adding zero real
information. Only a coarse bucket (``PRIMARY_PROB_BUCKET_COL``, "how many
of a couple of threshold bands did this fire clear") is retained, per the
task's "50/100 points" framing: enough magnitude to distinguish a
bare-threshold fire from a very-confident one, without handing the
meta-model the primary's actual calibrated score to recalibrate.
``test_meta_entry_features.py`` asserts no raw probability column leaks in.

This module does NOT touch cost math (that's ``option_payoff_labels.py``'s
job exclusively) and does NOT call the real primary model or any live
config -- it accepts fire-eligibility and payoff-score as plain inputs so
it can be developed and tested against synthetic data before either exists
for real (see ``ml_pipeline_2/scripts/train_meta_entry_v1.py``).
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Mapping, Sequence

import numpy as np
import pandas as pd

from snapshot_app.core.velocity_features import VELOCITY_COLUMNS

log = logging.getLogger(__name__)

# ── Selected velocity/acceleration ("rate", not "level") columns ───────────
#
# Chosen from snapshot_app.core.velocity_features._ALL_OUTPUT_COLUMNS (see
# that module's own docstrings for exactly how each is computed). Every
# column below is either:
#   (a) a delta already normalised by elapsed minutes ("build rate" /
#       "compression rate" -- a true per-minute rate, not a cumulative
#       level), or
#   (b) a second-order difference of an already-first-order delta
#       ("acceleration" -- the rate of change OF a rate of change).
# Explicitly NOT selected: level/position features (adx_14,
# ctx_am_price_position, ctx_am_range_*, ctx_am_trend*), first-order-only
# deltas with no per-time normalisation (vel_*_delta_open/_30m/_60m --
# these mix "how big" with "how long it's had to build" and are closer to
# a level than a rate), and categorical direction flags
# (vel_pcr_trend_direction, ctx_am_*_direction/_side) which describe WHERE
# not HOW FAST.
SELECTED_VELOCITY_FEATURES: List[str] = [
    # OI build RATE (delta already divided by minutes_elapsed) for each side
    # -- proxies whether fresh positioning is still accumulating right now,
    # as opposed to vel_ce_oi_delta_open which just says how much has
    # accumulated in total since the open.
    "vel_ce_oi_build_rate",
    "vel_pe_oi_build_rate",
    # PCR ACCELERATION: last-15m PCR change minus the previous 15m's PCR
    # change -- a second derivative. Distinguishes "PCR is still swinging
    # harder" (repricing still building) from "PCR swung once and has
    # since flattened" (already priced in).
    "vel_pcr_acceleration",
    # IV compression RATE (IV delta since open / minutes elapsed) -- how
    # fast implied vol is being bid up or crushed right now, not how much
    # it has moved in total.
    "vel_iv_compression_rate",
    # Price ACCELERATION: last-30m move minus the prior 30m's move -- is
    # the underlying's move still speeding up (still building) or has it
    # already decelerated (repricing largely done)?
    "vel_price_acceleration",
    # Options-volume ACCELERATION: ratio of the last-30m volume change to
    # the prior-30m volume change -- a normalised second derivative of
    # options flow, not a raw volume level.
    "vel_options_vol_acceleration",
]

_unknown = [c for c in SELECTED_VELOCITY_FEATURES if c not in VELOCITY_COLUMNS]
if _unknown:
    raise RuntimeError(
        "meta_entry_features.SELECTED_VELOCITY_FEATURES references column(s) no longer produced "
        f"by snapshot_app.core.velocity_features.VELOCITY_COLUMNS: {_unknown}. The source module's "
        "column names changed -- go re-read it and update the selection above rather than let this "
        "silently drift (do not guess names)."
    )

# ── New engineered feature: streak length ───────────────────────────────────
BARS_SINCE_ELIGIBLE_COL = "bars_since_primary_first_eligible"

# ── Coarse (deliberately non-continuous) primary-probability feature ───────
PRIMARY_PROB_BUCKET_COL = "primary_prob_bucket"

# Two thresholds -> three possible bucket values (0, 1, 2) -- "a couple of
# threshold bands", matching the task's "coarse 2-3 bucket" framing. Chosen
# well above a typical fire floor (~0.5) so the buckets actually split real
# fires into low/medium/high-conviction bands rather than all landing in
# the same bucket.
DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS: tuple[float, ...] = (0.60, 0.80)

# Final, ordered column list the meta-model is trained on. Tests assert the
# assembly functions below emit EXACTLY this set -- in particular, no raw
# "primary_probability" (or similarly named continuous) column.
META_FEATURE_COLUMNS: List[str] = list(SELECTED_VELOCITY_FEATURES) + [
    BARS_SINCE_ELIGIBLE_COL,
    PRIMARY_PROB_BUCKET_COL,
]


def bars_since_primary_first_eligible(eligible: Sequence[bool]) -> list[int]:
    """For each bar, how many CONSECUTIVE prior bars were also eligible
    (primary probability above whatever fire threshold the caller used)
    before this one -- 0 if this is the first eligible bar of a fresh
    streak, or if the bar itself is not eligible.

    Pure, no I/O. `eligible` must already be in chronological order.

    Examples:
        [T, T, T]          -> [0, 1, 2]
        [F, F, F]          -> [0, 0, 0]
        [T, F, T, F]       -> [0, 0, 0, 0]
        [T, T, F, T, T, T] -> [0, 1, 0, 0, 1, 2]   (streak resets after the gap)
    """
    out: list[int] = []
    streak = 0
    for e in eligible:
        if bool(e):
            out.append(streak)
            streak += 1
        else:
            out.append(0)
            streak = 0
    return out


def primary_probability_bucket(
    probability: float,
    thresholds: Sequence[float] = DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS,
) -> int:
    """Coarse bucket for the primary model's fire-time probability --
    NEVER the raw continuous value (see module docstring for why). Returns
    the count of `thresholds` (ascending) that `probability` clears, i.e.
    an integer in [0, len(thresholds)]."""
    if probability is None or not math.isfinite(probability):
        return 0
    return int(sum(1 for t in thresholds if probability >= t))


def select_velocity_rate_features(row: Mapping[str, float]) -> Dict[str, float]:
    """Pull SELECTED_VELOCITY_FEATURES out of one already-computed velocity
    row (e.g. one call's output from
    ``snapshot_app.core.velocity_features.compute_velocity_features``).
    This does NOT recompute anything from raw OI/IV/price data -- it only
    selects columns that pipeline already produces. Missing columns
    (upstream schema drift) degrade to NaN rather than raising, matching
    ``train_entry_dhan_v3.build_feature_matrix``'s convention."""
    out: Dict[str, float] = {}
    for col in SELECTED_VELOCITY_FEATURES:
        val = row.get(col) if hasattr(row, "get") else None
        try:
            out[col] = float(val) if val is not None and not (isinstance(val, float) and math.isnan(val)) else float("nan")
        except (TypeError, ValueError):
            out[col] = float("nan")
    return out


def select_velocity_rate_features_df(df: pd.DataFrame) -> pd.DataFrame:
    """DataFrame counterpart of `select_velocity_rate_features`, matching
    the shape produced by
    ``snapshot_app.core.velocity_features.compute_per_bar_velocity_df``.
    Missing columns become an all-NaN column (with a warning), not a raised
    error, so a partial upstream feature outage degrades gracefully."""
    missing = [c for c in SELECTED_VELOCITY_FEATURES if c not in df.columns]
    if missing:
        log.warning(
            "select_velocity_rate_features_df: missing from input velocity frame (will be NaN): %s",
            missing,
        )
    out = pd.DataFrame(index=df.index)
    for col in SELECTED_VELOCITY_FEATURES:
        if col in df.columns:
            out[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            out[col] = np.nan
    return out


def build_meta_features(
    velocity_row: Mapping[str, float],
    *,
    primary_probability: float,
    bars_since_first_eligible: int,
    prob_bucket_thresholds: Sequence[float] = DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS,
) -> Dict[str, float]:
    """Assemble the final meta-model feature row for ONE primary-fire event:
    the rate/acceleration subset + the streak-length feature + the coarse
    bucketed primary probability. `bars_since_first_eligible` must already
    be computed by the caller over the full, chronologically-ordered bar
    sequence (via `bars_since_primary_first_eligible`) -- it is meaningless
    computed on a single row or a pre-filtered subset.

    Returns exactly the keys in `META_FEATURE_COLUMNS` -- in particular,
    never a raw continuous primary-probability column."""
    feats = select_velocity_rate_features(velocity_row)
    feats[BARS_SINCE_ELIGIBLE_COL] = float(bars_since_first_eligible)
    feats[PRIMARY_PROB_BUCKET_COL] = float(
        primary_probability_bucket(primary_probability, prob_bucket_thresholds)
    )
    return feats


def build_meta_features_df(
    velocity_df: pd.DataFrame,
    *,
    primary_probability: Sequence[float],
    bars_since_first_eligible: Sequence[int],
    prob_bucket_thresholds: Sequence[float] = DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS,
) -> pd.DataFrame:
    """DataFrame counterpart of `build_meta_features`. `primary_probability`
    and `bars_since_first_eligible` must be pre-aligned, same-length
    sequences (positionally, not by index) with `velocity_df`'s rows --
    `bars_since_first_eligible` in particular must have been computed by
    the caller over the FULL bar sequence, not recomputed here on a
    filtered subset (see `bars_since_primary_first_eligible`'s docstring).

    Returns a DataFrame with exactly `META_FEATURE_COLUMNS`."""
    n = len(velocity_df)
    if len(primary_probability) != n or len(bars_since_first_eligible) != n:
        raise ValueError(
            "build_meta_features_df: velocity_df, primary_probability and bars_since_first_eligible "
            f"must all be the same length (got {n}, {len(primary_probability)}, "
            f"{len(bars_since_first_eligible)})"
        )
    feats = select_velocity_rate_features_df(velocity_df).reset_index(drop=True)
    feats[BARS_SINCE_ELIGIBLE_COL] = [float(b) for b in bars_since_first_eligible]
    feats[PRIMARY_PROB_BUCKET_COL] = [
        float(primary_probability_bucket(p, prob_bucket_thresholds)) for p in primary_probability
    ]
    return feats


__all__ = [
    "SELECTED_VELOCITY_FEATURES",
    "BARS_SINCE_ELIGIBLE_COL",
    "PRIMARY_PROB_BUCKET_COL",
    "DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS",
    "META_FEATURE_COLUMNS",
    "bars_since_primary_first_eligible",
    "primary_probability_bucket",
    "select_velocity_rate_features",
    "select_velocity_rate_features_df",
    "build_meta_features",
    "build_meta_features_df",
]

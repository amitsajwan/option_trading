"""Two-independent-models direction-margin research (NIFTY, 2026-09-27).

Fifth direction-research track today, and the first to abandon the shared
structural property of the other ten historical attempts (see
``ml_pipeline_2.pipeline.conditional_direction``'s and
``ml_pipeline_2.pipeline.entry_quality_direction``'s module docstrings for
the full list -- every prior test, across six 2026-06/09 methodologies plus
four more today, framed "CE or PE?" as ONE joint decision: a single binary
classifier, a single weighted composite score, or a single rule-based
signal comparing the two sides). This module instead trains a call-specific
and a put-specific regressor INDEPENDENTLY, each predicting its own leg's
actual net (post-cost) return, then asks whether the DIFFERENCE between the
two predictions (``predicted_margin = call_pred - put_pred``) has real
ranking skill against the actual difference in outcomes
(``actual_margin = net_ce_return - net_pe_return``).

Two concrete, real pieces of evidence motivate this specific framing (see
the owning study script's module docstring for the full argument):
1. ``docs/EXIT_RISK_EXPERIMENTS_2026-05.md``'s documented CE/PE asymmetry
   (PE leg PF 0.81 vs CE leg PF 1.42 in the same window on real trades) --
   a joint, mirror-symmetric model could be blending this away.
2. The one validated positive result in this project's entire research arc
   (``ml_pipeline_2.scripts.train_entry_option_payoff_v1``, holdout Spearman
   rho=0.154) succeeded specifically by framing entry-timing as a graded
   REGRESSION, not a binary classifier -- nobody has applied that same
   regression framing to direction before this track.

Import-isolation constraint (matches every sibling direction-research
module): this module must NEVER import
``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, or anything under those
packages -- registry-recorded research only, not a change to the live
serving path. It DOES import
``strategy_app.market.snapshot_accessor.SnapshotAccessor`` -- a plain data
accessor, exactly as ``option_payoff_labels.py`` and
``entry_quality_direction.py`` already do.

Three families of function live here:

1. Raw per-leg feature extraction (``extract_leg_raw_row`` /
   ``build_leg_raw_frame``) -- pulls CE-side and PE-side level fields
   (IV, volume, OI, OI change, volume ratio) straight off a
   ``SnapshotAccessor``. Confirmed 2026-09-27 by inspecting
   ``training_view_nifty_option_pnl.csv.gz``'s real header: it carries
   ``ENTRY_FEATURES_V3`` (which already has SOME CE/PE velocity pairs --
   ``vel_ce_oi_delta_open``/``vel_pe_oi_delta_open`` etc, see
   ``CALL_ONLY_FEATURES``/``PUT_ONLY_FEATURES`` docstrings) plus
   ``atm_ce_return_1m``/``atm_pe_return_1m``, but NOT the raw LEVEL fields
   (``atm_ce_iv``, ``atm_ce_oi``, ``atm_ce_volume``, ``atm_ce_oi_change_30m``,
   ``atm_ce_vol_ratio`` and their PE equivalents) that
   ``strategy_app/market/snapshot_accessor.py`` exposes directly -- those
   have to be re-read from the raw snapshot parquet, exactly the same
   re-extraction pattern ``entry_quality_direction.build_raw_signal_frame``
   already uses for a different field set.
2. Margin arithmetic (``compute_actual_margin`` / ``compute_predicted_margin``
   / ``margin_to_side`` / ``called_leg_actual_return``) -- the pure,
   NaN-safe primitives behind this track's central test: does the SIGN of
   ``predicted_margin`` carry real, magnitude-graded ranking skill against
   ``actual_margin``.
3. Walk-forward/evaluation utilities (``carve_inner_validation``,
   ``select_confident_subset_mask``, ``is_promising``) -- small, pure
   helpers the owning training script needs for its per-window HPO split
   and its "was this worth a delayed-action stress test" decision gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from strategy_app.market.snapshot_accessor import SnapshotAccessor

CE = "CE"
PE = "PE"

# ── Raw per-leg LEVEL fields absent from the option-pnl training view ──────
# All ten read straight off SnapshotAccessor's `atm_options` block (see
# strategy_app/market/snapshot_accessor.py lines ~484-537, confirmed
# 2026-09-27). Deliberately does NOT include atm_ce_close/atm_pe_close (raw
# premium levels, already the entry price used to compute the payoff label
# itself -- including them as a FEATURE would hand the model information
# that is one arithmetic step from its own target) or atm_ce_open/high/low
# (same objection, one step removed).
LEG_RAW_FEATURE_COLUMNS: List[str] = [
    "atm_ce_iv", "atm_pe_iv",
    "atm_ce_volume", "atm_pe_volume",
    "atm_ce_oi", "atm_pe_oi",
    "atm_ce_oi_change_30m", "atm_pe_oi_change_30m",
    "atm_ce_vol_ratio", "atm_pe_vol_ratio",
]

# ── Leg-specific additions to the shared ENTRY_FEATURES_V3 set ─────────────
# ENTRY_FEATURES_V3 (ml_pipeline_2.scripts.train_entry_dhan_v3) already
# contains SOME per-side pairs (vel_ce_oi_delta_open/vel_pe_oi_delta_open,
# vel_ce_oi_delta_30m/vel_pe_oi_delta_30m, vel_ce_oi_build_rate/
# vel_pe_oi_build_rate, vel_atm_ce_iv_delta_open/vel_atm_pe_iv_delta_open,
# vel_ce_vol_delta_30m/vel_pe_vol_delta_30m) -- those are shared verbatim by
# BOTH the call and put model (fed symmetrically, matching the task's "full
# shared ENTRY_FEATURES_V3 feature set" instruction). The lists below are
# the ADDITIONAL, OWN-LEG-ONLY features layered on top of that shared set:
# one already-joined column from the training view itself
# (atm_ce_return_1m/atm_pe_return_1m -- confirmed present in
# training_view_nifty_option_pnl.csv.gz's real header) plus the five raw
# LEVEL fields from LEG_RAW_FEATURE_COLUMNS this module adds by re-reading
# the raw snapshot parquet.
CALL_ONLY_FEATURES: List[str] = [
    "atm_ce_return_1m",
    "atm_ce_iv", "atm_ce_volume", "atm_ce_oi", "atm_ce_oi_change_30m", "atm_ce_vol_ratio",
]
PUT_ONLY_FEATURES: List[str] = [
    "atm_pe_return_1m",
    "atm_pe_iv", "atm_pe_volume", "atm_pe_oi", "atm_pe_oi_change_30m", "atm_pe_vol_ratio",
]


def extract_leg_raw_row(snap: SnapshotAccessor, *, bar_index: int) -> Dict[str, Any]:
    """One row of LEG_RAW_FEATURE_COLUMNS plus join/identity keys for a
    single snapshot. Shaped exactly like
    ``build_option_pnl_training_view``'s label-frame rows (``timestamp`` is
    the raw ISO string, ``trade_date`` the accessor's own) so this can feed
    the same ``join_features_to_labels`` join that module already uses --
    reused, not re-derived, for the join itself."""
    row: Dict[str, Any] = {
        "trade_date": snap.trade_date,
        "timestamp": snap.raw_payload.get("timestamp"),
        "bar_index": bar_index,
    }
    for col in LEG_RAW_FEATURE_COLUMNS:
        row[col] = getattr(snap, col)
    return row


def build_leg_raw_frame(snaps: Sequence[SnapshotAccessor]) -> pd.DataFrame:
    """Raw per-leg-feature frame for one chronological day of snapshots.
    ``bar_index`` is the 0-based position within `snaps` (assumed already
    sorted, matching ``read_day_snapshots``' sort-by-timestamp contract) --
    the same identity key the delayed-action stress test later needs to
    re-locate this exact bar inside a freshly re-read day's snapshot list."""
    cols = ["trade_date", "timestamp", "bar_index"] + LEG_RAW_FEATURE_COLUMNS
    if not snaps:
        return pd.DataFrame(columns=cols)
    rows = [extract_leg_raw_row(s, bar_index=i) for i, s in enumerate(snaps)]
    return pd.DataFrame(rows)[cols]


# ── Margin arithmetic ────────────────────────────────────────────────────

def compute_actual_margin(
    net_ce_return: Sequence[Optional[float]], net_pe_return: Sequence[Optional[float]],
) -> np.ndarray:
    """``net_ce_return - net_pe_return``, elementwise -- NaN wherever EITHER
    input is missing (never imputed, matching every other label in this
    pipeline). A positive value means the call leg actually outperformed
    the put leg at this bar; negative means the reverse."""
    ce = pd.to_numeric(pd.Series(net_ce_return), errors="coerce").to_numpy(dtype=float)
    pe = pd.to_numeric(pd.Series(net_pe_return), errors="coerce").to_numpy(dtype=float)
    return ce - pe


def compute_predicted_margin(
    call_pred: Sequence[float], put_pred: Sequence[float],
) -> np.ndarray:
    """``call_model.predict(bar) - put_model.predict(bar)``, elementwise.
    Both models always produce a prediction (features are median-filled
    before scoring, matching train_entry_option_payoff_v1's convention), so
    this is defined everywhere both models were scored -- unlike
    ``compute_actual_margin``, which is only defined where BOTH legs had a
    real, tradeable outcome."""
    a = np.asarray(call_pred, dtype=float)
    b = np.asarray(put_pred, dtype=float)
    if len(a) != len(b):
        raise ValueError(f"compute_predicted_margin: call_pred and put_pred must be same length ({len(a)} vs {len(b)})")
    return a - b


def margin_to_side(margin: Sequence[float], *, epsilon: float = 0.0) -> pd.Series:
    """CE where margin > epsilon, PE where margin < -epsilon, None
    (ambiguous / exactly flat) otherwise -- mirrors
    ``conditional_direction``'s CE/PE/None vote convention exactly, so this
    can be scored directly by
    ``breakout_agreement_direction.evaluate_signal_accuracy`` and
    ``conditional_direction.accuracy_and_coverage`` without adaptation."""
    m = pd.to_numeric(pd.Series(margin), errors="coerce")
    out = np.where(m > epsilon, CE, np.where(m < -epsilon, PE, None))
    return pd.Series(out, index=m.index, dtype=object)


def called_leg_actual_return(
    net_ce_return: Sequence[Optional[float]],
    net_pe_return: Sequence[Optional[float]],
    called_side: Sequence[Optional[str]],
) -> np.ndarray:
    """The net (post-cost) return of whichever leg `called_side` actually
    calls at each row -- CE -> net_ce_return, PE -> net_pe_return, None (or
    an unresolved leg) -> NaN. This is the "would trading this bar's call
    have paid" series the delayed-action stress test needs, deliberately
    NOT `max(net_ce_return, net_pe_return)` (the direction-agnostic oracle
    score) -- the call can well be the LOSING side even when the other leg
    would have paid, matching
    ``breakout_agreement_direction.leg_return_for_side``'s same design
    choice for the crossover/breakout tracks."""
    ce = pd.to_numeric(pd.Series(net_ce_return), errors="coerce").to_numpy(dtype=float)
    pe = pd.to_numeric(pd.Series(net_pe_return), errors="coerce").to_numpy(dtype=float)
    side = np.asarray(called_side, dtype=object)
    if not (len(ce) == len(pe) == len(side)):
        raise ValueError("called_leg_actual_return: all three inputs must be the same length")
    out = np.full(len(side), np.nan, dtype=float)
    out[side == CE] = ce[side == CE]
    out[side == PE] = pe[side == PE]
    return out


# ── Walk-forward per-window HPO split ───────────────────────────────────────

def carve_inner_validation(
    sorted_unique_dates: Sequence[str], *, valid_frac: float = 0.15, min_valid_days: int = 5,
) -> Tuple[str, str]:
    """Split a window's own TRAIN date range into an inner-train tail and an
    inner-validation head-of-tail, by DATE fraction (never by row count --
    intraday bar density can vary day to day, and splitting by date keeps
    whole days on one side, avoiding any same-day leakage across the split).
    Returns (inner_train_end_date, inner_valid_start_date) -- the caller
    filters rows with `trade_date <= inner_train_end_date` for HPO training
    and `trade_date >= inner_valid_start_date` (== the day right after) for
    HPO validation scoring. Raises if there are too few days to carve out a
    non-trivial validation slice."""
    dates = list(sorted_unique_dates)
    n = len(dates)
    n_valid = max(min_valid_days, int(round(n * valid_frac)))
    if n - n_valid < min_valid_days:
        raise ValueError(
            f"only {n} days available -- can't carve out {n_valid} for inner-validation and still "
            f"leave >= {min_valid_days} for inner-training"
        )
    inner_train_end = dates[n - n_valid - 1]
    inner_valid_start = dates[n - n_valid]
    return inner_train_end, inner_valid_start


def select_confident_subset_mask(predicted_margin: Sequence[float], *, frac: float = 0.10) -> np.ndarray:
    """Boolean mask selecting the top `frac` (most confidently CE) UNION
    bottom `frac` (most confidently PE) of `predicted_margin` by rank --
    the "confident calls" subset the delayed-action stress test re-scores,
    mirroring train_entry_option_payoff_v1's own "top-decile bars" fires
    convention (see its module docstring) extended symmetrically to both
    tails since this is a two-sided signal, not a one-sided fire/no-fire
    one."""
    m = np.asarray(predicted_margin, dtype=float)
    n = len(m)
    if n == 0:
        return np.zeros(0, dtype=bool)
    k = max(1, int(round(n * frac)))
    order = np.argsort(m, kind="mergesort")
    mask = np.zeros(n, dtype=bool)
    mask[order[:k]] = True   # most PE-favoring (lowest margin)
    mask[order[-k:]] = True  # most CE-favoring (highest margin)
    return mask


@dataclass(frozen=True)
class PromisingVerdict:
    promising: bool
    reasons: List[str]


def is_promising(
    *,
    pooled_margin_spearman_rho: Optional[float],
    pooled_margin_spearman_p: Optional[float],
    pooled_top_decile_mean_actual_margin: Optional[float],
    pooled_bottom_decile_mean_actual_margin: Optional[float],
    pooled_binary_accuracy: Optional[float],
    pooled_binary_permutation_p: Optional[float],
    per_window_binary_accuracy: Sequence[Optional[float]],
    spearman_rho_min: float = 0.05,
    spearman_p_max: float = 0.05,
    binary_accuracy_min: float = 0.55,
    binary_permutation_p_max: float = 0.05,
    min_windows_above_chance_frac: float = 0.6,
) -> PromisingVerdict:
    """Decision gate for "does this result clear the bar for a mandatory
    delayed-action stress test" -- ANY of: (a) a real pooled Spearman
    correlation between predicted and actual margin, (b) a real decile
    spread (top decile favors CE, bottom decile favors PE, both with the
    expected sign), or (c) pooled binary sign-accuracy above a real edge
    threshold AND walk-forward consistent (most individual windows also
    land above chance, not just the pooled number driven by one outlier
    window). Pure and testable -- the owning script's actual thresholds are
    passed in explicitly rather than hardcoded here, matching this
    pipeline's ship-gate convention (train_entry_option_payoff_v1.SHIP_GATE_*
    / breakout_agreement_direction's DEFAULT_EDGE_THRESHOLD)."""
    reasons: List[str] = []

    spearman_ok = (
        pooled_margin_spearman_rho is not None and pooled_margin_spearman_p is not None
        and pooled_margin_spearman_rho >= spearman_rho_min and pooled_margin_spearman_p <= spearman_p_max
    )
    if spearman_ok:
        reasons.append(
            f"pooled margin Spearman rho={pooled_margin_spearman_rho:.4f} "
            f"(p={pooled_margin_spearman_p:.4f}) clears >= {spearman_rho_min}"
        )

    decile_ok = (
        pooled_top_decile_mean_actual_margin is not None and pooled_bottom_decile_mean_actual_margin is not None
        and pooled_top_decile_mean_actual_margin > 0 and pooled_bottom_decile_mean_actual_margin < 0
    )
    if decile_ok:
        reasons.append(
            f"pooled decile spread: top={pooled_top_decile_mean_actual_margin:.4f} (>0), "
            f"bottom={pooled_bottom_decile_mean_actual_margin:.4f} (<0) -- correctly signed on both tails"
        )

    windows_with_acc = [a for a in per_window_binary_accuracy if a is not None]
    frac_above_chance = (
        sum(1 for a in windows_with_acc if a > 0.5) / len(windows_with_acc) if windows_with_acc else 0.0
    )
    binary_ok = (
        pooled_binary_accuracy is not None and pooled_binary_permutation_p is not None
        and pooled_binary_accuracy >= binary_accuracy_min and pooled_binary_permutation_p <= binary_permutation_p_max
        and frac_above_chance >= min_windows_above_chance_frac
    )
    if binary_ok:
        reasons.append(
            f"pooled binary accuracy={pooled_binary_accuracy:.4f} (p={pooled_binary_permutation_p:.4f}) "
            f"clears >= {binary_accuracy_min}, and {frac_above_chance:.0%} of windows are above chance"
        )

    return PromisingVerdict(promising=bool(spearman_ok or decile_ok or binary_ok), reasons=reasons)


__all__ = [
    "LEG_RAW_FEATURE_COLUMNS",
    "CALL_ONLY_FEATURES",
    "PUT_ONLY_FEATURES",
    "extract_leg_raw_row",
    "build_leg_raw_frame",
    "compute_actual_margin",
    "compute_predicted_margin",
    "margin_to_side",
    "called_leg_actual_return",
    "carve_inner_validation",
    "select_confident_subset_mask",
    "PromisingVerdict",
    "is_promising",
]

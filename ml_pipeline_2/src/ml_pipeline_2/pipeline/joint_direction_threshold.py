"""Absolute-probability threshold calibration for the joint direction
model's CALL output (NIFTY, 2026-09-27) -- refinement of the sixth
direction-research track (``ml_pipeline_2.pipeline.joint_direction`` /
``ml_pipeline_2.scripts.train_joint_direction_v1``), not a new architecture.

That track's headline number (pooled CALL top-decile mean +0.32%,
Spearman rho positive in 5/5 walk-forward windows) used ``argmax`` as the
trading rule and then took the top decile WITHIN the argmax==CALL subset --
a decile boundary that is a different absolute P(CALL) value in every
window (each window's model is independently fit, with its own probability
calibration), so "top decile" is not the same operating point across
windows and can't by itself answer "is there ONE threshold that would have
worked consistently." This module answers the narrower, more actionable
question directly: sweep P(CALL) as a plain, ABSOLUTE score threshold
(``P(CALL) >= t``), independent of whether CALL happens to be the model's
argmax at that bar, and check whether any single value of ``t`` is
profitable on the SAME absolute scale in most/all of the 5 walk-forward
windows -- the standard "decile-lift table done properly with an absolute
threshold" the owning task asks for.

Three families of function live here, all pure and independently testable
with synthetic fixtures (no model, no snapshot parquet needed):

1. Threshold grid + evaluation (``pooled_percentile_thresholds``,
   ``evaluate_threshold_mean``, ``bootstrap_mean_ci``,
   ``permutation_test_threshold_mean``, ``evaluate_threshold_with_stats``,
   ``sweep_thresholds``) -- the core "fire rate / mean actual return /
   is it positive / is it statistically real" table for ONE absolute
   threshold, generalized with an optional ``base_mask`` so the exact same
   evaluation function answers both item 1 (``base_mask=None``, the plain
   P(CALL) sweep) and item 2 (``base_mask=`` the entry-timing model's own
   top-quantile filter, from ``entry_quality_mask`` below) of the owning
   task -- "does requiring entry-timing agreement change whether a CALL
   threshold is profitable" is then just the same function called twice,
   compared side by side, not a second parallel implementation.

   ``permutation_test_threshold_mean`` mirrors
   ``direction_trust_meta.permutation_test_lift``'s design exactly (permute
   the SCORE array, keep the threshold value and the outcome array fixed,
   so the fired index set changes every draw but the cutoff itself doesn't)
   -- adapted for a continuous mean-return outcome instead of a binary
   accuracy lift, and extended with the same ``base_mask`` so a permutation
   restricted to an already-entry-filtered universe answers "given you're
   already in the entry-quality-filtered set, does the CALL threshold still
   carry information, or would any random same-sized subset of that same
   filtered set have done as well."  ``bootstrap_mean_ci`` is a plain
   percentile bootstrap of a subset's mean -- the continuous-outcome analog
   of ``direction_trust_meta.bootstrap_lift_ci`` (which is specific to a
   binary accuracy lift and not directly reusable for a continuous return).

2. Cross-window consistency (``consistency_summary``) -- the actual
   headline check item 1 of the owning task asks for: not "is the pooled
   mean positive" (track 6 already showed that can be driven entirely by
   one thin, early window) but "is it positive in most/all of the 5
   INDEPENDENT windows separately."

3. Entry-timing-filter combination (``entry_quality_mask``) and
   concentration/regime diagnostics for item 3 of the owning task
   (``date_concentration``, ``near_expiry_share``, ``regime_diagnostics``)
   -- is an apparent early-window effect broad-based or an artifact of a
   handful of correlated trade_dates or an unusually calm VIX regime that
   later windows simply didn't repeat.

Import-isolation constraint (matches every sibling direction-research
module today): must NEVER import ``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.brain
.regime_director``, or ``strategy_app.market.regime``. Registry-recorded
research only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

DEFAULT_PERCENTILES: Tuple[float, ...] = (
    50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 90.0, 92.0, 94.0, 95.0, 96.0, 97.0, 98.0, 99.0,
)


def pooled_percentile_thresholds(
    scores: Sequence[float], percentiles: Sequence[float] = DEFAULT_PERCENTILES,
) -> List[float]:
    """Sorted, de-duplicated list of absolute threshold VALUES at each of
    `percentiles` of `scores`'s own distribution -- computed ONCE on a
    pooled (or otherwise reference) score distribution and then applied as
    the SAME fixed values to every window, which is what makes "is this
    threshold consistent across windows" a meaningful question (a
    per-window-relative percentile would put every window at its own
    top-X%, guaranteeing apparent "consistency" by construction and
    defeating the point of the test)."""
    arr = pd.to_numeric(pd.Series(scores), errors="coerce").to_numpy(dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) == 0:
        return []
    vals = sorted({round(float(np.percentile(arr, p)), 6) for p in percentiles})
    return vals


def evaluate_threshold_mean(
    scores: Sequence[float],
    actual: Sequence[Optional[float]],
    threshold: float,
    *,
    base_mask: Optional[Sequence[bool]] = None,
    min_rows: int = 20,
) -> Dict[str, Any]:
    """Fire-rate / mean-actual-return readout for ONE absolute threshold on
    `scores` (e.g. P(CALL)), restricted first to `base_mask` when given
    (e.g. an entry-timing top-quantile filter) -- the single evaluation
    function both item 1 (base_mask=None) and item 2 (base_mask=entry
    filter) of the owning task are built from. `actual` should already be
    the economically-relevant outcome for the class being thresholded (e.g.
    `net_ce_return` for a P(CALL) sweep) -- this function itself is
    target-agnostic. Returns a dict with an internal `_fire_mask` key
    (boolean array, popped by callers that need to run further stats on the
    exact same fired set -- never serialize it to JSON as-is)."""
    scores_arr = np.asarray(scores, dtype=float)
    actual_arr = pd.to_numeric(pd.Series(actual), errors="coerce").to_numpy(dtype=float)
    n_total = len(scores_arr)
    if len(actual_arr) != n_total:
        raise ValueError(f"evaluate_threshold_mean: scores and actual must be the same length ({n_total} vs {len(actual_arr)})")
    universe = np.ones(n_total, dtype=bool) if base_mask is None else np.asarray(base_mask, dtype=bool)
    if len(universe) != n_total:
        raise ValueError(f"evaluate_threshold_mean: base_mask must be the same length as scores ({n_total} vs {len(universe)})")
    fire_mask = universe & (scores_arr >= threshold)
    n_universe = int(universe.sum())
    n_fired = int(fire_mask.sum())
    valid_mask = fire_mask & ~np.isnan(actual_arr)
    n_valid = int(valid_mask.sum())
    mean_actual = float(actual_arr[valid_mask].mean()) if n_valid > 0 else None
    return {
        "threshold": float(threshold),
        "n_total": n_total,
        "n_universe": n_universe,
        "n_fired": n_fired,
        "n_valid": n_valid,
        "fire_rate_of_universe": round(n_fired / n_universe, 4) if n_universe else 0.0,
        "fire_rate_of_all": round(n_fired / n_total, 4) if n_total else 0.0,
        "mean_actual": round(mean_actual, 5) if mean_actual is not None else None,
        "positive": bool(mean_actual is not None and mean_actual > 0.0),
        "thin_sample": n_valid < min_rows,
        "_fire_mask": fire_mask,
    }


def bootstrap_mean_ci(
    values: Sequence[float], *, n_boot: int = 2000, seed: int = 42, ci: float = 0.95,
) -> Tuple[Optional[float], Optional[float]]:
    """Plain percentile bootstrap CI of `values`'s own mean -- the
    continuous-outcome analog of `direction_trust_meta.bootstrap_lift_ci`
    (which is hard-coded to a binary accuracy lift against a baseline and
    isn't directly reusable for a continuous per-bar return); same
    resample-with-replacement-then-recompute-the-statistic design."""
    arr = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    arr = arr[~np.isnan(arr)]
    n = len(arr)
    if n == 0:
        return None, None
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, n)
        boots[i] = arr[idx].mean()
    alpha = (1.0 - ci) / 2.0
    lo, hi = np.quantile(boots, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def permutation_test_threshold_mean(
    scores: Sequence[float],
    actual: Sequence[Optional[float]],
    threshold: float,
    observed_mean: Optional[float],
    *,
    base_mask: Optional[Sequence[bool]] = None,
    n_perm: int = 2000,
    seed: int = 43,
) -> Optional[float]:
    """One-sided permutation p-value for `evaluate_threshold_mean`'s
    `mean_actual` at a FIXED threshold value. Mirrors
    `direction_trust_meta.permutation_test_lift`'s exact design: permute the
    SCORE array (not `actual`), recompute the fired mask against the SAME
    threshold each draw, and ask how often a random pairing does at least
    as well as what was actually observed -- the null being "this score
    carries no real relationship to the outcome." Restricted to `base_mask`
    when given, so the permutation only ever shuffles scores among bars
    that were already in the entry-filtered universe (never pulls in bars
    the filter would have excluded), directly testing item 2's "does the
    CALL threshold still carry information given you're already inside the
    entry-quality filter."""
    scores_arr = np.asarray(scores, dtype=float)
    actual_arr = pd.to_numeric(pd.Series(actual), errors="coerce").to_numpy(dtype=float)
    n = len(scores_arr)
    if observed_mean is None:
        return None
    universe_idx = np.arange(n) if base_mask is None else np.where(np.asarray(base_mask, dtype=bool))[0]
    if len(universe_idx) == 0:
        return None
    rng = np.random.default_rng(seed)
    sub_scores = scores_arr[universe_idx]
    sub_actual = actual_arr[universe_idx]
    count_ge = 0
    valid = 0
    for _ in range(n_perm):
        perm_scores = rng.permutation(sub_scores)
        mask = perm_scores >= threshold
        if not mask.any():
            continue
        vals = sub_actual[mask]
        vals = vals[~np.isnan(vals)]
        if len(vals) == 0:
            continue
        valid += 1
        if vals.mean() >= observed_mean:
            count_ge += 1
    if not valid:
        return None
    return (count_ge + 1) / (valid + 1)


def evaluate_threshold_with_stats(
    scores: Sequence[float],
    actual: Sequence[Optional[float]],
    threshold: float,
    *,
    base_mask: Optional[Sequence[bool]] = None,
    min_rows: int = 20,
    n_boot: int = 2000,
    n_perm: int = 2000,
    seed: int = 42,
) -> Dict[str, Any]:
    """`evaluate_threshold_mean` plus a bootstrap CI and permutation
    p-value on its `mean_actual` -- the full per-threshold row this
    module's sweep tables are built from. Stats are skipped (None) for a
    thin/empty fired set rather than computed on too little data to mean
    anything."""
    base = evaluate_threshold_mean(scores, actual, threshold, base_mask=base_mask, min_rows=min_rows)
    fire_mask = base.pop("_fire_mask")
    actual_arr = pd.to_numeric(pd.Series(actual), errors="coerce").to_numpy(dtype=float)
    fired_valid = actual_arr[fire_mask]
    fired_valid = fired_valid[~np.isnan(fired_valid)]
    ci_lo: Optional[float] = None
    ci_hi: Optional[float] = None
    p_value: Optional[float] = None
    if len(fired_valid) >= 2:
        ci_lo, ci_hi = bootstrap_mean_ci(fired_valid, n_boot=n_boot, seed=seed)
        p_value = permutation_test_threshold_mean(
            scores, actual, threshold, base["mean_actual"], base_mask=base_mask, n_perm=n_perm, seed=seed + 1,
        )
    base["ci_lo"] = round(ci_lo, 5) if ci_lo is not None else None
    base["ci_hi"] = round(ci_hi, 5) if ci_hi is not None else None
    base["permutation_p_value"] = round(p_value, 4) if p_value is not None else None
    return base


def sweep_thresholds(
    scores: Sequence[float], actual: Sequence[Optional[float]], thresholds: Sequence[float], **kwargs: Any,
) -> List[Dict[str, Any]]:
    """`evaluate_threshold_with_stats` applied across a whole threshold
    grid -- the per-window (or pooled) decile-lift-style table."""
    return [evaluate_threshold_with_stats(scores, actual, t, **kwargs) for t in thresholds]


def consistency_summary(
    per_window_rows: Sequence[Dict[str, Any]], *, recent_n: Optional[int] = None,
) -> Dict[str, Any]:
    """Given one `evaluate_threshold_with_stats`-shaped dict PER WINDOW (in
    chronological window order, all evaluated at the SAME threshold), is
    this threshold profitable CONSISTENTLY across windows -- the actual
    headline question item 1 of the owning task asks, as distinct from "is
    the pooled mean positive" (track 6 already showed the pooled CALL mean
    can be positive purely because one thin early window's mean is large
    enough to outweigh four flat-to-negative ones). A thin-sample window is
    excluded from the consistency fractions (mirrors this pipeline's
    existing convention of never letting a thin sample masquerade as a
    verdict either way) but always reported in `means_by_window` for
    transparency. `recent_n` (e.g. 4) additionally reports whether the
    MOST RECENT `recent_n` windows specifically are all positive -- the
    owning task's explicit fallback bar ("or at least the 4 most recent,
    larger ones") when full 5/5 consistency isn't met."""
    n_windows = len(per_window_rows)
    usable = [r for r in per_window_rows if not r.get("thin_sample") and r.get("mean_actual") is not None]
    n_usable = len(usable)
    n_positive = sum(1 for r in usable if r.get("positive"))
    frac_positive = (n_positive / n_usable) if n_usable else 0.0
    all_positive = n_usable > 0 and n_usable == n_windows and n_positive == n_usable

    recent_all_positive: Optional[bool] = None
    recent_rows: List[Dict[str, Any]] = []
    if recent_n:
        recent_rows = list(per_window_rows[-recent_n:])
        recent_usable = [r for r in recent_rows if not r.get("thin_sample") and r.get("mean_actual") is not None]
        recent_all_positive = (
            len(recent_rows) == recent_n and len(recent_usable) == recent_n and all(r.get("positive") for r in recent_usable)
        )

    return {
        "n_windows": n_windows,
        "n_usable_windows": n_usable,
        "n_positive_windows": n_positive,
        "frac_positive_of_usable": round(frac_positive, 4),
        "all_windows_positive": all_positive,
        "recent_n": recent_n,
        "recent_all_positive": recent_all_positive,
        "means_by_window": [r.get("mean_actual") for r in per_window_rows],
        "thin_sample_by_window": [bool(r.get("thin_sample")) for r in per_window_rows],
    }


# ── Entry-timing-filter combination (item 2) ────────────────────────────────

def entry_quality_mask(entry_scores: Sequence[float], top_quantile: float) -> np.ndarray:
    """Boolean mask selecting the TOP `(1 - top_quantile)` fraction of
    `entry_scores` -- e.g. `top_quantile=0.75` selects the top quartile,
    `0.90` the top decile. Stated as a quantile cutoff (not a decile-table
    row) because it needs to compose with an arbitrary P(CALL) threshold
    via `evaluate_threshold_mean`'s `base_mask`, not just a fixed decile-10
    bucket. NaN scores never qualify (mirrors this pipeline's
    drop-don't-impute convention)."""
    arr = pd.to_numeric(pd.Series(entry_scores), errors="coerce").to_numpy(dtype=float)
    n = len(arr)
    if n == 0:
        return np.zeros(0, dtype=bool)
    valid = ~np.isnan(arr)
    if not valid.any():
        return np.zeros(n, dtype=bool)
    cutoff = np.quantile(arr[valid], top_quantile)
    return valid & (arr >= cutoff)


# ── Concentration / regime diagnostics (item 3) ─────────────────────────────

def date_concentration(trade_dates: Sequence[Any], mask: Sequence[bool]) -> Dict[str, Any]:
    """For bars where `mask` is True: how many DISTINCT trade_dates do they
    span, and how concentrated are they in the top 1-3 dates -- item 3(b)'s
    "mostly one or two trade_dates" red flag, the exact pattern this
    project has been burned by before (a thin, correlated cluster of bars
    masquerading as a broad-based statistical result)."""
    dates_arr = np.asarray([str(d) for d in trade_dates], dtype=object)
    mask_arr = np.asarray(mask, dtype=bool)
    if len(dates_arr) != len(mask_arr):
        raise ValueError("date_concentration: trade_dates and mask must be the same length")
    sub = dates_arr[mask_arr]
    n = len(sub)
    if n == 0:
        return {
            "n_bars": 0, "n_distinct_dates": 0, "top_date": None,
            "top_date_share": None, "top3_dates_share": None, "date_counts": {},
        }
    counts = pd.Series(sub).value_counts()
    top_date = str(counts.index[0])
    top_share = float(counts.iloc[0] / n)
    top3_share = float(counts.iloc[: min(3, len(counts))].sum() / n)
    return {
        "n_bars": n,
        "n_distinct_dates": int(counts.shape[0]),
        "top_date": top_date,
        "top_date_share": round(top_share, 4),
        "top3_dates_share": round(top3_share, 4),
        "date_counts": {str(k): int(v) for k, v in counts.items()},
    }


def near_expiry_share(
    dte: Sequence[Optional[float]], mask: Sequence[bool], *, max_dte: float = 0.0,
) -> Dict[str, Any]:
    """Share of masked bars with `dte <= max_dte` (default: expiry-day
    bars) versus the SAME share in the full (unmasked) population -- item
    3(b)'s "mostly near-expiry days" check, reported as a comparison to a
    baseline rather than a raw percentage in isolation (a dataset that is
    already ~20% expiry-day bars overall makes a masked subset's raw 20%
    unremarkable; a masked subset at 70%+ against a ~20% baseline is the
    real signal)."""
    dte_arr = pd.to_numeric(pd.Series(dte), errors="coerce").to_numpy(dtype=float)
    mask_arr = np.asarray(mask, dtype=bool)
    if len(dte_arr) != len(mask_arr):
        raise ValueError("near_expiry_share: dte and mask must be the same length")
    valid_all = ~np.isnan(dte_arr)
    baseline = float((dte_arr[valid_all] <= max_dte).mean()) if valid_all.any() else None
    sub_valid = valid_all & mask_arr
    masked_share = float((dte_arr[sub_valid] <= max_dte).mean()) if sub_valid.any() else None
    return {
        "max_dte": max_dte,
        "baseline_share": round(baseline, 4) if baseline is not None else None,
        "masked_share": round(masked_share, 4) if masked_share is not None else None,
        "n_masked_valid": int(sub_valid.sum()),
    }


def regime_diagnostics(
    vix: Sequence[Optional[float]], adx: Sequence[Optional[float]],
) -> Dict[str, Any]:
    """Plain descriptive regime stats (VIX level/stability, ADX trend
    strength) for a window's test-period population -- item 3(a)'s "was
    there anything unusual about market conditions in that specific
    window" check, done with real numbers rather than a narrative guess. A
    window with a much lower `vix_std` than its siblings was in an unusually
    STABLE (not just calm-level) regime -- this project's own prior finding
    (`project_r1s_regime_finding.md`: "short-premium works in calm regimes
    only") is exactly the kind of regime-dependence this function makes
    checkable for the direction side too."""
    vix_arr = pd.to_numeric(pd.Series(vix), errors="coerce").to_numpy(dtype=float)
    adx_arr = pd.to_numeric(pd.Series(adx), errors="coerce").to_numpy(dtype=float)
    vix_valid = vix_arr[~np.isnan(vix_arr)]
    adx_valid = adx_arr[~np.isnan(adx_arr)]
    return {
        "n_vix": int(len(vix_valid)),
        "vix_mean": round(float(vix_valid.mean()), 4) if len(vix_valid) else None,
        "vix_std": round(float(vix_valid.std()), 4) if len(vix_valid) else None,
        "vix_min": round(float(vix_valid.min()), 4) if len(vix_valid) else None,
        "vix_max": round(float(vix_valid.max()), 4) if len(vix_valid) else None,
        "n_adx": int(len(adx_valid)),
        "adx_mean": round(float(adx_valid.mean()), 4) if len(adx_valid) else None,
        "adx_std": round(float(adx_valid.std()), 4) if len(adx_valid) else None,
    }


__all__ = [
    "DEFAULT_PERCENTILES",
    "pooled_percentile_thresholds",
    "evaluate_threshold_mean",
    "bootstrap_mean_ci",
    "permutation_test_threshold_mean",
    "evaluate_threshold_with_stats",
    "sweep_thresholds",
    "consistency_summary",
    "entry_quality_mask",
    "date_concentration",
    "near_expiry_share",
    "regime_diagnostics",
]

"""Entry-quality-conditioned direction research (NIFTY, 2026-09-26).

This project has tested unconditional CE-vs-PE direction prediction at
least six independent ways -- per-member signal accuracy, a 31-combination
ablation grid, a 2026 out-of-sample quorum test, order-flow proxies, a
direction split on high-ML-probability bars, and 125 real live trades --
and every one converged near a 50-56% coin flip (see
project_directional_buy_dead_2026-06-11.md and
project_direction_forensics_deep_dive_2026-09-12.md). This module does NOT
repeat that. It answers a narrower, previously-untested question: is
direction more callable specifically on the bars an INDEPENDENT entry-
timing model (``ml_pipeline_2.scripts.train_entry_option_payoff_v1``, which
predicts ``max(net CE return, net PE return)`` -- itself direction-agnostic)
rates as high quality? Every prior direction test conditioned on the
direction model's OWN confidence (circular) or on raw move size, never on
an independently-trained entry-quality score.

Two families of function live here:

1. Rule-signal replication (``vote_*`` / ``composite_vote``) -- faithful
   re-implementations of each member of
   ``strategy_app.ml.entry_direction_resolver.resolve_entry_direction_composite``'s
   scoring logic (weights/thresholds transcribed by hand from that file,
   2026-09-26 -- if its defaults change, these will silently drift and
   should be re-diffed by hand). This module deliberately does NOT import
   that resolver, or anything else under ``strategy_app/ml`` or
   ``strategy_app/engines/strategies`` -- this is registry-recorded
   research, not a change to the live serving path, and must stay
   import-isolated from it (see the study script's module docstring for
   the full list of files this research must never touch).

   Each ``vote_*`` function takes a DataFrame of RAW snapshot fields (the
   same fields ``SnapshotAccessor`` reads: ``fut_return_5m``,
   ``fut_return_15m``, ``price_vs_vwap``, ``vix_intraday_chg``,
   ``atm_ce_iv``, ``atm_pe_iv``, ``orh``, ``orl``, ``orh_broken``,
   ``orl_broken``, ``fut_close``, ``pcr_change_5m``) and returns a
   pandas Series of ``"CE"`` / ``"PE"`` / ``None`` -- one vote per bar,
   ``None`` where that signal doesn't fire (matches the resolver's own
   "skip if weight<=0 or value is 0/None" convention). Two resolver
   members have NO available proxy in this historical data and are
   documented, not silently approximated -- see ``OMITTED_SIGNALS``.

2. Entry-quality-conditioning utilities: rank-based decile assignment,
   per-signal accuracy-by-decile, walk-forward window construction, and a
   paired bootstrap/permutation significance test for comparing two
   models' accuracy on the SAME bars -- plus a direction-specific analog
   of ``validation.check_repricing_wall`` (``check_direction_repricing_wall``)
   for the mandatory delayed-action stress test this project's own
   discipline requires before trusting any apparent entry-timing edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

CE = "CE"
PE = "PE"

# ── Signals with no faithful proxy in this data ──────────────────────────────
OMITTED_SIGNALS: Dict[str, str] = {
    "depth": (
        "resolver's order-book terms (bid/ask imbalance, microprice) need live "
        "Redis depth snapshots (DepthContext), which are never persisted to the "
        "historical snapshot parquet this study reads."
    ),
    "ml_tilt": (
        "resolver's own small DIRECTION_ML_MODEL_PATH tilt term reads a live "
        "model path from env at call time -- not applicable to an offline "
        "research run, and re-including it here would be circular with this "
        "exact study's own conditioned-vs-unconditioned model comparison."
    ),
}

# ── Composite weights, transcribed verbatim from the resolver's env-var ─────
# DEFAULTS (ENTRY_DIR_W_*) in
# strategy_app/ml/entry_direction_resolver.py::resolve_entry_direction_composite,
# 2026-09-26. Depth and ml_tilt are excluded -- see OMITTED_SIGNALS.
COMPOSITE_WEIGHTS: Dict[str, float] = {
    "momentum_5m": 1.0,
    "momentum_15m": 0.55,
    "vwap_side": 0.65,
    "vix_chg": 0.75,
    "iv_skew": 0.5,
    "or_trap": 0.9,
    "pcr_chg": 0.4,
}
COMPOSITE_MIN_MARGIN = 0.35

VIX_CHG_THRESHOLD = 2.0
IV_SKEW_PE_RATIO_HIGH = 1.08   # ratio = pe_iv / ce_iv
IV_SKEW_CE_RATIO_LOW = 0.92
PCR_CHG_THRESHOLD = 0.02

REQUIRED_RAW_COLUMNS: List[str] = [
    "fut_return_5m", "fut_return_15m", "price_vs_vwap", "vix_intraday_chg",
    "atm_ce_iv", "atm_pe_iv", "orh", "orl", "orh_broken", "orl_broken",
    "fut_close", "pcr_change_5m",
]


def _sign_vote(values) -> pd.Series:
    """CE where >0, PE where <0, None where ==0 or NaN -- the resolver's
    shared `_score_signed(..., bullish_is_ce=True)` rule, used verbatim by
    momentum_5m/15m and vwap_side."""
    v = pd.to_numeric(pd.Series(values), errors="coerce")
    out = np.where(v > 0, CE, np.where(v < 0, PE, None))
    return pd.Series(out, index=v.index, dtype=object)


def vote_momentum_5m(df: pd.DataFrame) -> pd.Series:
    return _sign_vote(df["fut_return_5m"])


def vote_momentum_15m(df: pd.DataFrame) -> pd.Series:
    return _sign_vote(df["fut_return_15m"])


def vote_vwap_side(df: pd.DataFrame) -> pd.Series:
    """Resolver: `price_vs_vwap > 0` -> CE, `< 0` -> PE. NOTE: `price_vs_vwap`
    was confirmed (2026-09-26, direct snapshot inspection across 2024-11
    .. 2026-07) to be universally None/NaN before roughly Q1 2026 and fully
    populated from ~2026-04 onward -- a real, project-wide data gap, not a
    limitation of this study. Coverage is reported per window; do not read a
    near-zero accuracy in an early window as "the signal is bad" when it may
    simply have near-zero coverage."""
    return _sign_vote(df["price_vs_vwap"])


def vote_vix_chg(df: pd.DataFrame, threshold: float = VIX_CHG_THRESHOLD) -> pd.Series:
    """Resolver: fires only when |vix_intraday_chg| >= threshold (default
    2.0); rising VIX -> PE (fear/hedging demand), falling VIX -> CE."""
    v = pd.to_numeric(df["vix_intraday_chg"], errors="coerce")
    out = np.where(v >= threshold, PE, np.where(v <= -threshold, CE, None))
    return pd.Series(out, index=v.index, dtype=object)


def vote_iv_skew(
    df: pd.DataFrame,
    *,
    pe_ratio_high: float = IV_SKEW_PE_RATIO_HIGH,
    ce_ratio_low: float = IV_SKEW_CE_RATIO_LOW,
) -> pd.Series:
    """Resolver: ratio = pe_iv/ce_iv; ratio > 1.08 -> PE (puts relatively
    richer), ratio < 0.92 -> CE (calls relatively richer). Requires both IVs
    positive, matching the resolver's own guard."""
    ce_iv = pd.to_numeric(df["atm_ce_iv"], errors="coerce")
    pe_iv = pd.to_numeric(df["atm_pe_iv"], errors="coerce")
    valid = (ce_iv > 0) & (pe_iv > 0)
    ratio = pe_iv / ce_iv.replace(0, np.nan)
    out = np.where(
        valid & (ratio > pe_ratio_high), PE,
        np.where(valid & (ratio < ce_ratio_low), CE, None),
    )
    return pd.Series(out, index=df.index, dtype=object)


def vote_or_trap(df: pd.DataFrame) -> pd.Series:
    """Resolver's opening-range false-break/trap signal: requires
    `or_ready` (both orh and orl resolved). `orl_broken` (price broke below
    the opening-range low earlier) AND current `fut_close > orl` (price has
    since recovered above it) -> CE (failed breakdown, bullish reversal
    read). Symmetric `orh_broken` + `fut_close < orh` -> PE. If BOTH fire at
    once (a same-day double-whipsaw through both bounds) the resolver adds
    to both scores with no net directional effect -- treated here as an
    ambiguous non-vote (None), matching that net-zero outcome rather than
    arbitrarily picking a side."""
    orh = pd.to_numeric(df["orh"], errors="coerce")
    orl = pd.to_numeric(df["orl"], errors="coerce")
    fut_close = pd.to_numeric(df["fut_close"], errors="coerce")
    orh_broken = df["orh_broken"].astype("boolean").fillna(False)
    orl_broken = df["orl_broken"].astype("boolean").fillna(False)
    or_ready = orh.notna() & orl.notna()

    ce_fires = or_ready & orl_broken & orl.notna() & fut_close.notna() & (fut_close > orl)
    pe_fires = or_ready & orh_broken & orh.notna() & fut_close.notna() & (fut_close < orh)
    both = ce_fires & pe_fires
    out = np.where(
        both, None,
        np.where(ce_fires, CE, np.where(pe_fires, PE, None)),
    )
    return pd.Series(out, index=df.index, dtype=object)


def vote_pcr_chg(df: pd.DataFrame, threshold: float = PCR_CHG_THRESHOLD) -> pd.Series:
    """Resolver: |pcr_change_5m| >= 0.02 gates; falling PCR (put unwinding /
    call buying) -> CE, rising PCR -> PE."""
    v = pd.to_numeric(df["pcr_change_5m"], errors="coerce")
    out = np.where(v <= -threshold, CE, np.where(v >= threshold, PE, None))
    return pd.Series(out, index=v.index, dtype=object)


SIGNAL_VOTERS = {
    "momentum_5m": vote_momentum_5m,
    "momentum_15m": vote_momentum_15m,
    "vwap_side": vote_vwap_side,
    "vix_chg": vote_vix_chg,
    "iv_skew": vote_iv_skew,
    "or_trap": vote_or_trap,
    "pcr_chg": vote_pcr_chg,
}


def composite_vote(
    df: pd.DataFrame,
    *,
    weights: Optional[Dict[str, float]] = None,
    min_margin: float = COMPOSITE_MIN_MARGIN,
) -> pd.Series:
    """Reduced composite: weighted sum of the 7 available signals' votes
    (excludes depth/ml_tilt, see OMITTED_SIGNALS), vetoed below `min_margin`
    or when both scores are non-positive -- same veto rule as the live
    resolver. This is NOT identical to the live composite (two of its nine
    terms are structurally unavailable here); treat it as "the reduced
    composite this historical data can support," not a drop-in replica."""
    w = weights or COMPOSITE_WEIGHTS
    ce_score = pd.Series(0.0, index=df.index)
    pe_score = pd.Series(0.0, index=df.index)
    for name, weight in w.items():
        votes = SIGNAL_VOTERS[name](df)
        ce_score = ce_score + np.where(votes.to_numpy() == CE, weight, 0.0)
        pe_score = pe_score + np.where(votes.to_numpy() == PE, weight, 0.0)
    margin = (ce_score - pe_score).abs()
    vetoed = (margin < min_margin) | ((ce_score <= 0) & (pe_score <= 0))
    direction = np.where(ce_score.to_numpy() >= pe_score.to_numpy(), CE, PE)
    out = np.where(vetoed.to_numpy(), None, direction)
    return pd.Series(out, index=df.index, dtype=object)


# ── Accuracy scoring ──────────────────────────────────────────────────────────

def accuracy_and_coverage(votes: pd.Series, truth: pd.Series) -> Dict[str, object]:
    """Accuracy over bars where the signal fired AND ground truth exists;
    `coverage` reports what fraction of all bars that was, since a signal
    that "only ever votes on 2% of bars, always correctly" is a very
    different finding from one that fires everywhere."""
    votes = pd.Series(votes).reset_index(drop=True)
    truth = pd.Series(truth).reset_index(drop=True)
    mask = votes.notna() & truth.notna()
    n_total = len(votes)
    n_voted = int(mask.sum())
    if n_voted == 0:
        return {"n_total": n_total, "n_voted": 0, "coverage": 0.0, "accuracy": None, "n_correct": 0}
    correct = int((votes[mask] == truth[mask]).sum())
    return {
        "n_total": n_total,
        "n_voted": n_voted,
        "coverage": round(n_voted / n_total, 4) if n_total else 0.0,
        "accuracy": round(correct / n_voted, 4),
        "n_correct": correct,
    }


def assign_predicted_deciles(predicted: np.ndarray, n_deciles: int = 10) -> np.ndarray:
    """Rank-based, equal-COUNT decile assignment (1 = lowest predicted
    value, n_deciles = highest) -- same bucketing convention as
    `train_entry_option_payoff_v1.decile_lift_table`, just returning a
    per-row label instead of an aggregated table. Ties broken by stable
    sort (original order)."""
    predicted = np.asarray(predicted, dtype=float)
    n = len(predicted)
    order = np.argsort(predicted, kind="mergesort")
    deciles = np.empty(n, dtype=int)
    edges = np.linspace(0, n, n_deciles + 1).astype(int)
    for i in range(n_deciles):
        lo, hi = edges[i], edges[i + 1]
        deciles[order[lo:hi]] = i + 1
    return deciles


def signal_accuracy_by_decile(
    df: pd.DataFrame,
    deciles: np.ndarray,
    *,
    truth_col: str = "payoff_winning_side",
    include_composite: bool = True,
) -> pd.DataFrame:
    """One row per (signal, decile): n_voted, coverage, accuracy. `deciles`
    must be aligned 1:1 with `df`'s row order (positional, not index-based)."""
    df = df.reset_index(drop=True)
    deciles = np.asarray(deciles)
    truth = df[truth_col]
    all_votes = {name: fn(df) for name, fn in SIGNAL_VOTERS.items()}
    if include_composite:
        all_votes["composite_proxy"] = composite_vote(df)
    rows = []
    for name, votes in all_votes.items():
        for d in sorted(set(deciles.tolist())):
            mask = deciles == d
            stats = accuracy_and_coverage(votes[mask].reset_index(drop=True), truth[mask].reset_index(drop=True))
            rows.append({"signal": name, "decile": int(d), **stats})
    return pd.DataFrame(rows)


# ── Walk-forward windows ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class WalkForwardWindow:
    window_id: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str


def build_expanding_windows(
    sorted_unique_dates: Sequence[str],
    *,
    n_windows: int,
    min_train_days: int,
) -> List[WalkForwardWindow]:
    """Expanding-window walk-forward split: the first `min_train_days`
    trading days seed window 1's training set; the remaining days are cut
    into `n_windows` roughly equal, SEQUENTIAL test chunks, with each
    window's training set expanding to include everything before its own
    test chunk (never after -- no leakage). Mirrors the "roll forward,
    never shuffle" discipline `windowing.build_day_folds` already applies
    elsewhere in this pipeline, reimplemented here as its own small pure
    function so this study doesn't take on a dependency on that module's
    purge/embargo semantics it doesn't need.
    """
    dates = list(sorted_unique_dates)
    if len(dates) <= min_train_days:
        raise ValueError(
            f"only {len(dates)} trading days available, need > min_train_days={min_train_days} "
            f"to carve out even one test window"
        )
    remaining = dates[min_train_days:]
    if len(remaining) < n_windows:
        raise ValueError(
            f"only {len(remaining)} days left after reserving {min_train_days} for the initial "
            f"training set -- not enough to form {n_windows} windows"
        )
    edges = np.linspace(0, len(remaining), n_windows + 1).astype(int)
    windows: List[WalkForwardWindow] = []
    for i in range(n_windows):
        lo, hi = edges[i], edges[i + 1]
        test_chunk = remaining[lo:hi]
        if not test_chunk:
            continue
        train_end_idx = min_train_days + lo - 1
        windows.append(
            WalkForwardWindow(
                window_id=i + 1,
                train_start=dates[0],
                train_end=dates[train_end_idx],
                test_start=test_chunk[0],
                test_end=test_chunk[-1],
            )
        )
    return windows


# ── Significance testing (paired accuracy comparison) ────────────────────────

@dataclass(frozen=True)
class PairedAccuracyComparison:
    n: int
    accuracy_a: Optional[float]
    accuracy_b: Optional[float]
    observed_diff: Optional[float]  # a - b
    bootstrap_ci_low: Optional[float]
    bootstrap_ci_high: Optional[float]
    permutation_p_value: Optional[float]


def paired_accuracy_comparison(
    correct_a: Sequence[bool],
    correct_b: Sequence[bool],
    *,
    n_boot: int = 2000,
    n_perm: int = 5000,
    seed: int = 42,
    ci: float = 0.95,
) -> PairedAccuracyComparison:
    """Compare two models' bar-by-bar correctness on the SAME bars (e.g. a
    conditioned vs. unconditioned direction classifier, both scored on the
    same top-decile test bars). Returns the observed accuracy-difference
    (a - b), a paired-bootstrap CI on that difference, and a two-sided
    sign-flip permutation-test p-value under the null "a and b are
    exchangeable on each bar" -- the right null for "does conditioning
    training data on entry quality change accuracy," since architecture and
    evaluation bars are held identical and only the training rows differ.
    """
    a = np.asarray(correct_a, dtype=bool)
    b = np.asarray(correct_b, dtype=bool)
    if len(a) != len(b):
        raise ValueError(f"correct_a and correct_b must be paired (same length): {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return PairedAccuracyComparison(0, None, None, None, None, None, None)

    acc_a = float(a.mean())
    acc_b = float(b.mean())
    observed_diff = acc_a - acc_b

    rng = np.random.default_rng(seed)
    diff_per_bar = a.astype(int) - b.astype(int)  # in {-1, 0, 1}

    # Paired bootstrap: resample bars with replacement, recompute mean diff.
    boot_diffs = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot_diffs[i] = diff_per_bar[idx].mean()
    alpha = (1.0 - ci) / 2.0
    ci_low = float(np.quantile(boot_diffs, alpha))
    ci_high = float(np.quantile(boot_diffs, 1.0 - alpha))

    # Sign-flip permutation test on the paired difference (exact analog of a
    # paired permutation test): under the null, each bar's +1/-1 contribution
    # to diff_per_bar is equally likely to have either sign.
    nonzero = diff_per_bar[diff_per_bar != 0]
    if len(nonzero) == 0:
        perm_p = 1.0
    else:
        observed_stat = abs(nonzero.sum())
        signs = rng.choice([-1, 1], size=(n_perm, len(nonzero)))
        perm_stats = np.abs((signs * nonzero).sum(axis=1))
        perm_p = float((perm_stats >= observed_stat).sum() + 1) / (n_perm + 1)

    return PairedAccuracyComparison(
        n=n,
        accuracy_a=round(acc_a, 4),
        accuracy_b=round(acc_b, 4),
        observed_diff=round(observed_diff, 4),
        bootstrap_ci_low=round(ci_low, 4),
        bootstrap_ci_high=round(ci_high, 4),
        permutation_p_value=round(perm_p, 4),
    )


# ── Direction-specific repricing-wall stress test ────────────────────────────

class DirectionRepricingWallFailure(ValueError):
    """Raised (when `raise_on_fail=True`) when an apparent entry-quality-
    conditioned direction edge collapses or inverts once the direction call
    is delayed 1-2 bars past the entry-payoff signal's own fire moment --
    the direction analog of `validation.check_repricing_wall`, motivated by
    the same three prior documented straddle/vertical failures (see that
    function's docstring): a signal that only "works" at the instant of
    peak model confidence is very likely measuring the option chain's own
    repricing of the same information, not a structural, tradeable pattern.
    """


@dataclass(frozen=True)
class DirectionRepricingWallReport:
    ok: bool
    n_paired_bars: int
    accuracy_at_fire: Optional[float] = None
    accuracy_delay_1: Optional[float] = None
    accuracy_delay_2: Optional[float] = None
    reason: str = ""


def check_direction_repricing_wall(
    *,
    votes_at_fire: Sequence[Optional[str]],
    truth_at_fire: Sequence[Optional[str]],
    votes_delay_1: Sequence[Optional[str]],
    truth_delay_1: Sequence[Optional[str]],
    votes_delay_2: Sequence[Optional[str]],
    truth_delay_2: Sequence[Optional[str]],
    min_paired_bars: int = 20,
    edge_threshold: float = 0.55,
    raise_on_fail: bool = True,
) -> DirectionRepricingWallReport:
    """Re-score the SAME fired bars' direction accuracy using the direction
    call AND ground truth both recomputed 1 and 2 bars after the original
    fire (ground truth recomputed with `compute_bar_payoff`'s fixed-strike,
    shortened-horizon convention -- same source function
    `run_repricing_wall_control.reprice_fires_at_delays` already uses for
    the payoff-level check, so the delayed "winning side" is genuinely
    "what would have won if you'd looked 1-2 bars later," not the original
    bar's answer). Only bars where all three (fire/+1/+2) vote+truth pairs
    are available are used -- dropped, never imputed, matching every other
    label in this pipeline.

    Fails when accuracy at fire is above `edge_threshold` (there's an
    apparent above-chance edge worth testing) but accuracy at +1 or +2
    falls back to/below chance (0.5) -- the exact collapse pattern that
    sank three prior payoff-level experiments, now checked for direction
    specifically. A signal with no edge at fire-time isn't this check's
    concern (reports ok=True with an explanatory reason).
    """
    triples = [
        (va, ta, v1, t1, v2, t2)
        for va, ta, v1, t1, v2, t2 in zip(
            votes_at_fire, truth_at_fire, votes_delay_1, truth_delay_1, votes_delay_2, truth_delay_2,
        )
        if va is not None and ta is not None and v1 is not None and t1 is not None and v2 is not None and t2 is not None
    ]
    n = len(triples)
    if n < min_paired_bars:
        reason = (
            f"only {n} bars have a vote+truth pair at fire/+1/+2 all available, need "
            f">= {min_paired_bars} to draw any conclusion -- a thin-sample non-result."
        )
        result = DirectionRepricingWallReport(ok=False, n_paired_bars=n, reason=reason)
        if raise_on_fail:
            raise DirectionRepricingWallFailure(reason)
        return result

    acc_fire = sum(1 for va, ta, *_ in triples if va == ta) / n
    acc_d1 = sum(1 for _, _, v1, t1, _, _ in triples if v1 == t1) / n
    acc_d2 = sum(1 for *_, v2, t2 in triples if v2 == t2) / n

    if acc_fire <= edge_threshold:
        reason = (
            f"accuracy at fire-time ({acc_fire:.4f} over {n} bars) is already at/below the "
            f"{edge_threshold:.2f} edge threshold -- no above-chance edge here to test for "
            f"collapse; this candidate should already be failing on the plain decile/holdout "
            f"comparison, not this check."
        )
        return DirectionRepricingWallReport(
            ok=True, n_paired_bars=n, accuracy_at_fire=round(acc_fire, 4),
            accuracy_delay_1=round(acc_d1, 4), accuracy_delay_2=round(acc_d2, 4), reason=reason,
        )

    collapsed = acc_d1 <= 0.5 or acc_d2 <= 0.5
    ok = not collapsed
    reason = "" if ok else (
        f"direction accuracy at fire-time (acc={acc_fire:.4f} over {n} bars) collapses to "
        f"acc={acc_d1:.4f} at +1 bar / acc={acc_d2:.4f} at +2 bars (<= chance) -- the same "
        f"repricing-wall collapse pattern documented for payoff-level edges "
        f"(MODEL_REBUILD_SPEC_2026-07-11.md Amendment 3/4). This apparent direction edge "
        f"very likely only exists at the exact instant of peak model confidence and should "
        f"NOT be trusted as a structural, tradeable pattern."
    )
    result = DirectionRepricingWallReport(
        ok=ok, n_paired_bars=n, accuracy_at_fire=round(acc_fire, 4),
        accuracy_delay_1=round(acc_d1, 4), accuracy_delay_2=round(acc_d2, 4), reason=reason,
    )
    if not ok and raise_on_fail:
        raise DirectionRepricingWallFailure(reason)
    return result

"""Big-move-agreement + fade-the-breakout direction research (NIFTY, 2026-09-27).

Third of three direction-research tracks run 2026-09-27:
  - commit 3a9f1455a2 (``entry_quality_direction.py`` /
    ``study_entry_quality_conditioned_direction.py``): is direction more
    predictable on bars an INDEPENDENT entry-timing model rates highly? Clean
    negative across 5 walk-forward windows.
  - commit 1ef111b614 (``direction_trust_meta.py`` /
    ``study_direction_trust_meta.py``): can a meta-model predict when the
    composite resolver's call can be trusted? Clean negative (meta-AUC
    0.49-0.52 every window).

This track is different from both: it does not condition an existing
unconditional direction call on anything. It tests whether a SPECIFIC,
DIFFERENT rule -- not the composite resolver's weighted blend -- has real
standalone accuracy. It targets the ONE directional claim cited repeatedly
across this project's historical docs but never properly validated: "vwap +
PCR/max_pain agreement on BIG moves (>=150-160 points on BankNifty) predicts
direction at ~59-62%" (``docs/archive/strategy_platform_old/
DIRECTION_TREE_FINDINGS.md``, restated in ``docs/strategy_platform/
DIRECTION_STRATEGY_SYNTHESIS.md``), plus a second, independently-documented
pattern: breakout CONTINUATION is anti-predictive (~41.3% OOS) while FADING
the breakout scores ~59% (``docs/archive/entry_model/
COMPRESSION_STATE_ENGINE_PLAN.md``, ``docs/archive/entry_model/
BMM_RESULTS.md``).

Both historical findings share the same flaw this module exists to fix: thin
samples (83-181 bars over 8 days), never bootstrap/permutation-tested, never
walk-forward validated, and (per ``MODEL_REBUILD_SPEC_2026-07-11.md``
Amendment 4) never survived a delayed-action ("foresight is priced in")
stress test when actually trade-tested (-208pts/-Rs6,252 at 5min, 30 trades,
despite the claimed ~57% directional accuracy holding at fire-time).

Import-isolation constraint (matches every direction module built today):
this module must NEVER import ``strategy_app.ml.entry_direction_resolver``,
``strategy_app.brain.regime_director``, ``strategy_app.market.regime``, or
``strategy_app.engines.strategies.ml_entry`` -- registry-recorded research
only. ``strategy_app.market.regime.RegimeClassifier._classify_trend_vs_sideways``
was READ (not imported) to hand-transcribe the breakout direction/confirmation
logic below -- if its defaults change, this will silently drift and should be
re-diffed by hand, exactly the discipline ``conditional_direction.
COMPOSITE_WEIGHTS`` already documents for the composite resolver.

Two signal variants, both built ONLY from columns already present in
``training_view_nifty_direction_condition.csv.gz`` (the tracks-1/2 sibling
``build_entry_quality_direction_dataset.py``'s output):

1. ``big_move_agreement_vote`` -- reconstructs "vwap + PCR agreement on big
   moves" from the SAME two raw signals ``conditional_direction.py``'s
   ``vote_vwap_side``/``vote_pcr_chg`` already vote on (``price_vs_vwap``,
   ``pcr_change_5m``). ``max_pain`` itself has NO column anywhere in this
   historical archive -- a real data gap, not a design choice, so this
   variant is "vwap + PCR agreement", not "vwap + PCR/max_pain agreement" as
   literally described historically; documented here, not silently
   substituted (matching ``conditional_direction.OMITTED_SIGNALS``'
   convention). "Big move" is measured causally via trailing
   ``|fut_return_15m|`` -- the SAME trailing-return column the composite
   resolver's own ``momentum_15m`` member reads -- deliberately NOT the
   forward-looking ``fwd_maxabs_*`` columns also present in this training
   view (those are look-ahead-only labeling artifacts a live decision could
   never see at fire time). Multiple thresholds are tested because the
   original ``DIRECTION_TREE_FINDINGS.md`` figure (150-160 points on
   BankNifty's ~52-54k spot at the time) is ITSELF already expressed as a
   spot-relative percentage in that very document (80pt/0.15%, 108pt/0.20%,
   160pt/0.30%) -- so sweeping a percentage-of-spot grid directly reproduces
   the original study's own units on NIFTY (~24k spot), rather than
   inventing a new scaling convention.

2. ``breakout_fade_vote`` / ``breakout_continuation_vote`` -- reconstructs
   the opening-range BREAKOUT direction call exactly as
   ``strategy_app.market.regime.RegimeClassifier._classify_trend_vs_sideways``
   computes it: direction is BULL (CE) when ``orh_broken``, BEAR (PE) when
   ``orl_broken`` -- BULL wins the tie-break when BOTH are set in the same
   bar, a real quirk of the live code's literal
   ``direction = "BULL" if snap.orh_broken else "BEAR"`` (checked
   unconditionally, not ``elif``), faithfully reproduced here rather than
   "fixed" (this project's standing discipline: evaluate the real live logic,
   bugs and quirks included, not a hypothetically-cleaner version of it --
   see ``direction_trust_meta.py``'s module docstring re: composite's own
   dead VIX signal). Confirmed by (a) a wide-enough opening range
   (``orw_pct = (orh-orl)/orl >= REGIME_BREAKOUT_ORW_PCT_MIN``, the exact
   constant transcribed from ``regime.py``'s own
   ``REGIME_BREAKOUT_ORW_PCT_MIN`` env-var default) and (b) strong volume.
   Live ``regime.py`` gates that second condition on ``vol_ratio`` (a
   SnapshotAccessor property backed by ``futures_derived.vol_ratio``), which
   this historical training view does NOT carry. It DOES carry
   ``vol_spike_ratio`` -- a different, but analogous, volume-surge feature
   from the same compression-features block (current-bar volume vs its own
   20-bar rolling average, vs. ``vol_ratio``'s own different rolling-window
   definition) -- substituted here at the SAME numeric cutoff ``regime.py``
   uses for ITS (different) volume gate
   (``REGIME_TREND_VOL_RATIO_MIN=1.30``), flagged as a substitution, not a
   re-derivation. Both ``vol_spike_ratio`` and ``price_vs_vwap`` are
   confirmed (2026-09-27, direct inspection) to be populated only from
   2025-11-03 onward in this archive (42.8% overall coverage) -- a real,
   project-wide data gap matching ``conditional_direction.vote_vwap_side``'s
   own documented note about ``price_vs_vwap``; per-window coverage MUST be
   reported alongside accuracy so an early window's near-zero fire rate
   isn't misread as "the signal doesn't work" when it may simply not exist
   yet. ``breakout_continuation_vote`` (the raw, un-flipped breakout call)
   is reported alongside the fade version specifically because
   ``BMM_RESULTS.md``'s claim is that continuation and fading DIVERGE (~41%
   vs ~59%) -- both numbers are needed to confirm or refute that on this
   data, not the fade side in isolation.

Statistics: ``evaluate_signal_accuracy`` is a NEW single-sample test
(bootstrap CI + Monte-Carlo permutation p-value against a FIXED null of
50%) -- deliberately not reusing ``conditional_direction.
paired_accuracy_comparison`` (a paired TWO-MODEL test) or
``direction_trust_meta.evaluate_trust_filter`` (a lift-over-the-SAME-
population test): this study's question is simpler -- "is this signal's OWN
fired-bar accuracy above a flat 50% coin flip" -- and needs its own null,
not a comparison against another model's calls on the same bars.

``leg_return_for_side`` scores the SPECIFIC leg a signal calls (not
``BarPayoff.payoff_score``'s direction-agnostic ``max(net_ce, net_pe)``) --
required for the decisive delayed-action payoff test
(``ml_pipeline_2.scripts.study_breakout_agreement_direction``), which mirrors
``ml_pipeline_2.scripts.run_repricing_wall_control``'s exact design (re-price
the SAME fired bars 1-2 bars later, holding the same target exit) applied to
one specific direction call instead of an entry-timing model's top decile.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .conditional_direction import (
    CE,
    PE,
    PCR_CHG_THRESHOLD,
    accuracy_and_coverage,
    vote_pcr_chg,
    vote_vwap_side,
)
from .option_payoff_labels import BarPayoff

# ── Variant A: big-move + vwap/pcr agreement ─────────────────────────────────

# Percentage-of-spot thresholds for |fut_return_15m|. Reproduces
# DIRECTION_TREE_FINDINGS.md's own three brackets (80pt/0.15%, 108pt/0.20%,
# 160pt/0.30% on BankNifty's ~53k spot at the time -- ALREADY spot-relative
# percentages in that document, not points this module re-scales), plus one
# below (0.10%, to check whether "small moves = coin flip" also replicates)
# and one interpolating the top bracket (0.25%).
BIG_MOVE_PCT_THRESHOLDS: Tuple[float, ...] = (0.0010, 0.0015, 0.0020, 0.0025, 0.0030)

# Trailing (causal) 15-minute return -- composite's own momentum_15m input.
# Deliberately NOT any `fwd_maxabs_*` column (those are forward/look-ahead
# labeling artifacts, unusable at live fire-time).
MOVE_MAGNITUDE_COLUMN = "fut_return_15m"


def big_move_agreement_vote(
    df: pd.DataFrame,
    *,
    move_threshold: float,
    move_col: str = MOVE_MAGNITUDE_COLUMN,
    pcr_threshold: float = PCR_CHG_THRESHOLD,
) -> pd.Series:
    """CE/PE where (a) trailing ``|move_col|`` >= ``move_threshold`` AND (b)
    vwap-side and pcr-change-side agree on a direction; None (abstain)
    otherwise -- reconstructs DIRECTION_TREE_FINDINGS.md's decision tree
    ("big move + vwap AND pcr agree -> directional; else -> straddle/abstain")
    at real statistical rigor instead of the original 83-181-bar hand
    search."""
    move = pd.to_numeric(df[move_col], errors="coerce").abs()
    big = (move >= move_threshold).to_numpy()
    vwap_vote = vote_vwap_side(df)
    pcr_vote = vote_pcr_chg(df, threshold=pcr_threshold)
    agree = (vwap_vote.notna() & (vwap_vote == pcr_vote)).to_numpy()
    fires = big & agree
    out = np.where(fires, vwap_vote.to_numpy(), None)
    return pd.Series(out, index=df.index, dtype=object)


# ── Variant B: fade-the-breakout ─────────────────────────────────────────────

# Transcribed verbatim from strategy_app.market.regime.py's real defaults
# (RegimeClassifier.__init__: REGIME_BREAKOUT_ORW_PCT_MIN,
# REGIME_TREND_VOL_RATIO_MIN env vars), 2026-09-27 -- if those change, this
# will silently drift and should be re-diffed by hand, same discipline as
# conditional_direction.COMPOSITE_WEIGHTS.
BREAKOUT_ORW_PCT_MIN = 0.003
# regime.py's real volume gate reads `vol_ratio` (no column of that name
# exists in this historical archive). Substituted with `vol_spike_ratio` (a
# different, analogous volume-surge feature already in the training view --
# see module docstring) at regime.py's OWN numeric cutoff for its own
# (different) volume gate. A documented substitution, not a re-derivation.
BREAKOUT_VOL_SPIKE_MIN = 1.30


def opening_range_width_pct(df: pd.DataFrame) -> pd.Series:
    """``(orh-orl)/orl`` -- verbatim reproduction of
    ``SnapshotAccessor.opening_range_width_pct``'s formula."""
    orh = pd.to_numeric(df["orh"], errors="coerce")
    orl = pd.to_numeric(df["orl"], errors="coerce")
    safe_orl = orl.where(orl > 0.0)
    return (orh - safe_orl) / safe_orl


def breakout_direction_vote(
    df: pd.DataFrame,
    *,
    require_confirmation: bool = True,
    vol_spike_min: float = BREAKOUT_VOL_SPIKE_MIN,
    orw_min: float = BREAKOUT_ORW_PCT_MIN,
) -> pd.Series:
    """CE ("BULL") where ``orh_broken``, PE ("BEAR") where ``orl_broken`` and
    NOT ``orh_broken`` -- BULL wins when both are set, exactly matching
    regime.py's real ``"BULL" if snap.orh_broken else "BEAR"`` tie-break (a
    faithful reproduction of that quirk, not a fix -- see module docstring).
    None where neither is broken, or (when ``require_confirmation``, the
    default) where the confirmation gate (``vol_spike_ratio >= vol_spike_min``
    AND ``orw_pct >= orw_min``) fails -- matching regime.py's real BREAKOUT-
    regime gate (see module docstring for the ``vol_ratio`` ->
    ``vol_spike_ratio`` substitution). ``require_confirmation=False`` is the
    unconfirmed ablation (raw orX_broken alone, "has today broken either
    bound at all") used to show how much the confirmation gate matters --
    orX_broken alone fires on the large majority of afternoon bars (~58-59%
    each individually, ~78% either, confirmed 2026-09-27) and is NOT by
    itself the selective, rare signal this task's brief expects."""
    orh_broken = df["orh_broken"].astype("boolean").fillna(False)
    orl_broken = df["orl_broken"].astype("boolean").fillna(False)
    any_broken = (orh_broken | orl_broken).to_numpy()
    direction = np.where(orh_broken.to_numpy(), CE, PE)
    fires = any_broken.copy()
    if require_confirmation:
        vol = pd.to_numeric(df["vol_spike_ratio"], errors="coerce")
        orw = opening_range_width_pct(df)
        confirmed = ((vol >= vol_spike_min) & (orw >= orw_min)).to_numpy()
        fires = fires & confirmed
    out = np.where(fires, direction, None)
    return pd.Series(out, index=df.index, dtype=object)


def _flip_side(vote: pd.Series) -> pd.Series:
    values = vote.to_numpy()
    flipped = np.where(values == CE, PE, np.where(values == PE, CE, None))
    return pd.Series(flipped, index=vote.index, dtype=object)


def breakout_continuation_vote(df: pd.DataFrame, **kwargs) -> pd.Series:
    """The raw breakout direction call itself (alias over
    ``breakout_direction_vote``) -- reported alongside the fade version to
    test BMM_RESULTS.md's specific claim that continuation and fading
    DIVERGE (~41% vs ~59%), not just to report the fade number alone."""
    return breakout_direction_vote(df, **kwargs)


def fade_breakout_vote(df: pd.DataFrame, **kwargs) -> pd.Series:
    """Opposite side of ``breakout_direction_vote`` -- fires on exactly the
    same bars, calling the opposite direction."""
    return _flip_side(breakout_direction_vote(df, **kwargs))


# ── Called-leg payoff extraction (for the delayed-action stress test) ──────

def leg_return_for_side(bp: Optional[BarPayoff], side: Optional[str]) -> Optional[float]:
    """Net (post-cost) return of the SPECIFIC leg ``side`` calls (CE or PE)
    from a ``BarPayoff`` -- None if that side has no usable quote, or either
    argument is None. Deliberately NOT ``bp.payoff_score``
    (direction-agnostic ``max(net_ce, net_pe)``) -- this study needs the
    return of the side the SIGNAL actually called, which can well be the
    LOSING side even when the other leg would have paid."""
    if bp is None or side is None:
        return None
    leg = bp.ce if side == CE else bp.pe if side == PE else None
    return leg.net_return if leg is not None else None


# ── Statistics: single-sample accuracy vs a fixed baseline ─────────────────

@dataclass(frozen=True)
class SingleSampleAccuracyTest:
    n: int
    accuracy: Optional[float]
    bootstrap_ci_low: Optional[float]
    bootstrap_ci_high: Optional[float]
    permutation_p_value: Optional[float]


def bootstrap_accuracy_ci(
    correct: Sequence[bool], *, n_boot: int = 2000, seed: int = 42, ci: float = 0.95,
) -> Tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap CI on a single signal's own fired-bar accuracy
    (resample bars with replacement, recompute the mean each time)."""
    arr = np.asarray(correct, dtype=bool)
    n = len(arr)
    if n == 0:
        return None, None
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot[i] = arr[idx].mean()
    alpha = (1.0 - ci) / 2.0
    lo, hi = np.quantile(boot, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def permutation_test_vs_baseline(
    correct: Sequence[bool], *, null_accuracy: float = 0.5, n_perm: int = 5000, seed: int = 43,
) -> Optional[float]:
    """One-sided Monte-Carlo permutation p-value against a FIXED null
    accuracy (default 0.5, the unconditional coin-flip every prior direction
    test in this project converges to): the fraction of ``n_perm`` synthetic
    samples of ``n`` independent Bernoulli(``null_accuracy``) draws whose
    mean accuracy is >= the observed one. This is the right null for "is
    this signal's fired-bar accuracy above chance" -- NOT a comparison
    against another model's calls on the same bars (that is what
    ``conditional_direction.paired_accuracy_comparison`` is for)."""
    arr = np.asarray(correct, dtype=bool)
    n = len(arr)
    if n == 0:
        return None
    observed = float(arr.mean())
    rng = np.random.default_rng(seed)
    sim = rng.random((n_perm, n)) < null_accuracy
    sim_means = sim.mean(axis=1)
    p = (float(np.sum(sim_means >= observed)) + 1.0) / (n_perm + 1.0)
    return float(p)


def evaluate_signal_accuracy(
    votes: pd.Series,
    truth: pd.Series,
    *,
    null_accuracy: float = 0.5,
    n_boot: int = 2000,
    n_perm: int = 5000,
    seed: int = 42,
) -> Dict[str, object]:
    """Combines ``conditional_direction.accuracy_and_coverage`` with a
    bootstrap CI + permutation p-value against ``null_accuracy`` -- the full
    per-window (or pooled) report this study needs for each signal
    variant/threshold: how often it fires (coverage), how accurate it is
    when it does (accuracy), and whether that accuracy is a real,
    statistically-supported edge over a flat coin flip rather than a lucky
    thin sample."""
    votes = pd.Series(votes).reset_index(drop=True)
    truth = pd.Series(truth).reset_index(drop=True)
    base = accuracy_and_coverage(votes, truth)
    mask = (votes.notna() & truth.notna()).to_numpy()
    correct = (votes[mask].to_numpy() == truth[mask].to_numpy())
    ci_lo, ci_hi = bootstrap_accuracy_ci(correct, n_boot=n_boot, seed=seed)
    p_value = permutation_test_vs_baseline(correct, null_accuracy=null_accuracy, n_perm=n_perm, seed=seed + 1)
    return {
        **base,
        "bootstrap_ci_low": round(ci_lo, 4) if ci_lo is not None else None,
        "bootstrap_ci_high": round(ci_hi, 4) if ci_hi is not None else None,
        "permutation_p_value": round(p_value, 4) if p_value is not None else None,
    }


# ── Variant overlap / agreement ─────────────────────────────────────────────

def variant_agreement_report(vote_a: pd.Series, vote_b: pd.Series, truth: pd.Series) -> Dict[str, object]:
    """Where variant A (big-move-agreement) and variant B (fade-the-breakout)
    BOTH fire: how often do they call the SAME side, and how does each
    perform on that shared subset? Answers this task's explicit "report
    whether they agree or diverge on the bars where both fire" requirement."""
    a = pd.Series(vote_a).reset_index(drop=True)
    b = pd.Series(vote_b).reset_index(drop=True)
    t = pd.Series(truth).reset_index(drop=True)
    both_fire = (a.notna() & b.notna())
    n_both = int(both_fire.sum())
    if n_both == 0:
        return {"n_both_fired": 0, "agreement_rate": None,
                "accuracy_variant_a_on_shared_bars": None,
                "accuracy_variant_b_on_shared_bars": None,
                "accuracy_when_variants_agree": None}
    a_sub = a[both_fire]
    b_sub = b[both_fire]
    t_sub = t[both_fire]
    agree_mask = (a_sub.to_numpy() == b_sub.to_numpy())
    n_agree = int(agree_mask.sum())
    acc_a = float((a_sub.to_numpy() == t_sub.to_numpy()).mean())
    acc_b = float((b_sub.to_numpy() == t_sub.to_numpy()).mean())
    acc_when_agree = None
    if n_agree > 0:
        acc_when_agree = float((a_sub.to_numpy()[agree_mask] == t_sub.to_numpy()[agree_mask]).mean())
    return {
        "n_both_fired": n_both,
        "agreement_rate": round(n_agree / n_both, 4),
        "accuracy_variant_a_on_shared_bars": round(acc_a, 4),
        "accuracy_variant_b_on_shared_bars": round(acc_b, 4),
        "accuracy_when_variants_agree": round(acc_when_agree, 4) if acc_when_agree is not None else None,
    }


__all__ = [
    "BIG_MOVE_PCT_THRESHOLDS",
    "MOVE_MAGNITUDE_COLUMN",
    "BREAKOUT_ORW_PCT_MIN",
    "BREAKOUT_VOL_SPIKE_MIN",
    "big_move_agreement_vote",
    "opening_range_width_pct",
    "breakout_direction_vote",
    "breakout_continuation_vote",
    "fade_breakout_vote",
    "leg_return_for_side",
    "SingleSampleAccuracyTest",
    "bootstrap_accuracy_ci",
    "permutation_test_vs_baseline",
    "evaluate_signal_accuracy",
    "variant_agreement_report",
]

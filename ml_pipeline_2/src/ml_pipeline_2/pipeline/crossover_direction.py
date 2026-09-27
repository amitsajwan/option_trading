"""Classic discretionary-trader crossover direction research (NIFTY, 2026-09-27).

Fourth of four direction-research tracks run today, and the ninth independent
methodology this project has applied to CE-vs-PE direction prediction overall
-- the first eight (per-member signal accuracy, a 31-combination ablation
grid, a 2026 OOS quorum test, order-flow proxies, a direction split on
high-ML-probability bars, 125 real live trades, entry-quality conditioning
(``entry_quality_direction.py``), trust meta-labeling (``direction_trust_meta
.py``), and a definitive big-move/fade-breakout re-test
(``breakout_agreement_direction.py``)) all converged near a 50-56% coin flip.
This track deliberately tests a DIFFERENT signal family never tried before in
this project: classic technical-analysis CROSSOVER events (EMA golden/death
cross, MACD line/signal cross, RSI level crosses) -- what a discretionary
chart trader would actually mean by "crossover", not a blended composite
score (that family is already exhausted, see ``conditional_direction
.composite_vote``).

Feature-pipeline provenance (verified 2026-09-27 by a full read of
``snapshot_app/core/feature_engine.py``, per this study's own brief, PLUS a
follow-up check of the raw snapshot archive after an initial pass of this
study incorrectly assumed feature_engine.py was the only place MACD could
live -- see below):
  - RSI-14 (``osc_rsi_14``) IS computed by the live feature pipeline
    (``feature_engine._rsi``, Wilder's smoothing, ``ewm(alpha=1/14,
    adjust=False, min_periods=1)``, reset every trading day because
    ``build_features`` always runs on a single day's bars) -- but was never
    given a column in ``training_view_nifty_direction_condition.csv.gz`` or
    any other training view (confirmed by inspecting that CSV's real header:
    it has ``ema_9``/``ema_21``/``ema_50``/``adx_14``/``atr_14_1m``/
    ``bb_width_20`` but no ``rsi``/``osc_rsi_14`` column at all). This module
    extracts RSI-14 fresh, reusing ``feature_engine._rsi`` VERBATIM (imported,
    not re-derived) for bit-for-bit fidelity with the live definition.
  - MACD is NOT computed ANYWHERE in ``feature_engine.py`` itself (confirmed
    by a full read -- no `macd` string appears in that module at all).
    HOWEVER, a full-text search of ``snapshot_app/`` (prompted by the same
    brief's instruction to verify rather than assume) found a SECOND,
    separate computation path: ``snapshot_app/core/market_snapshot.py``'s
    ``_macd_last`` DOES compute a real 12/26/9 MACD, but only on a
    resampled 5-MINUTE close series, and stores it under each snapshot's own
    ``mtf_derived.macd_line_5m`` / ``mtf_derived.macd_signal_5m`` /
    ``mtf_derived.macd_hist_5m`` fields -- confirmed genuinely populated in
    the real historical snapshot archive (~55% coverage per day, consistent
    across every sampled date from 2024-11-04 to 2026-07-31; the ~45% gap is
    the 5-minute EMA-26's own warm-up, roughly the first ~2 hours of every
    session, not a partial rollout). ``mtf_derived`` also carries
    ``rsi_14_5m`` (81% coverage), ``ema_9_5m``/``ema_21_5m`` (89%/73%),
    and ``bb_pct_b_5m`` (75%, a Bollinger %B already computed on 5-minute
    bars) -- all reused VERBATIM here (read straight from the archive, not
    recomputed) as a second, genuinely-multi-timeframe signal family
    alongside the fresh 1-minute indicators this module also computes
    (`compute_rsi_14`/`compute_macd`/`compute_bollinger_bands`). This is a
    real correction to this module's own first-pass claim that "MACD is not
    computed anywhere in this codebase" -- it exists, just not in
    ``feature_engine.py``, and not on the 1-minute timeframe. (``mtf_derived``
    also carries 15-minute variants -- ``ema_50_15m`` is confirmed
    STRUCTURALLY DEAD, 0% coverage in every sampled day, because it resets
    every trading date and a ~375-minute session cannot accumulate the 750
    minutes a 50-period 15-minute EMA needs to ever produce a non-null
    value; the 15-minute family is not used by this study for that reason.)
  - The fresh 1-minute MACD this module ALSO computes (``compute_macd``,
    reusing ``feature_engine._ema``) remains useful because it is a
    genuinely different, faster timeframe than the archive's 5-minute
    version -- both are tested, reported separately, per this study's own
    "classic crossover, multiple timeframes a discretionary trader might
    actually watch" scope.

Truncation/warm-up note: ``training_view_nifty_direction_condition.csv.gz``
truncates every trading day to ``bar_index`` 30-350 (09:45-15:05, the entry
label's session window) -- computing a 26-period EMA (MACD's slow leg)
directly on that truncated series would carry a real, material warm-up bias
into every day's first ~30-50 rows (bar_index 30-~80). This module's
functions therefore expect a FULL trading day's bars (``bar_index`` 0 onward,
09:15 session start) built from the raw snapshot archive
(``build_full_day_frame``, mirroring ``entry_quality_direction
.build_raw_signal_frame``'s per-day-from-snapshots pattern) -- the owning
script (``ml_pipeline_2.scripts.study_crossover_direction``) computes every
indicator and crossover event over the FULL day, THEN restricts to the
labeled ``bar_index>=30`` window only for evaluation against
``payoff_winning_side``. EMA levels themselves are read directly from each
snapshot's own precomputed ``ema_9``/``ema_21``/``ema_50`` fields (the exact
source of the training view's own ema columns -- not recomputed), so
EMA-crossover detection is bit-identical to what the live serving path would
see.

Day-boundary discipline: every crossover/level-cross detector compares a bar
only to the PRECEDING bar of the SAME ``trade_date`` (a ``same_day`` mask via
``trade_date.shift(1)`` equality) -- yesterday's closing state never bleeds
into today's first bar. This matches ``feature_engine.py``'s own day-reset
convention for ``ema_9``/``ema_21``/``ema_50``/``osc_rsi_14``/``osc_atr_14``
(each computed on a single-day DataFrame, never a multi-day concatenation).
A side effect: the FIRST bar of every trading day can never register a cross
(no valid previous-bar state exists within that day) -- an intentional,
uniform, small loss of detectable events (not a selective bias), not a bug.

Import-isolation constraint (matches every direction module built today):
this module must NEVER import ``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.brain
.regime_director``, or ``strategy_app.market.regime`` -- registry-recorded
research only, no coupling to the live serving path. It DOES reuse
``snapshot_app.core.feature_engine``'s ``_rsi``/``_ema`` primitives (the
shared training+live feature pipeline explicitly named in this study's own
brief as the place to verify/extract from) and (via the owning script only)
``strategy_app.market.snapshot_accessor.SnapshotAccessor`` for raw-field
reads -- plain data utilities, not the resolver.

Five signal families, each returning a CE/PE/None vote per bar (fires
rarely, by design -- a crossover is a discrete event, not a persistent
state, unlike the already-tested static-spread-sign features ``ema_order``/
``ema_above_21``):
  1. ``ema_cross_vote`` -- EMA 9/21 and EMA 21/50 crossover ("golden
     cross"/"death cross"): CE the bar the faster EMA crosses ABOVE the
     slower one, PE the bar it crosses BELOW.
  2. ``macd_cross_vote`` -- MACD line crossing its own 9-period EMA signal
     line: CE on a bullish cross, PE on a bearish cross.
  3. ``rsi_trend_cross_vote`` -- RSI-14 crossing the 50 midline: CE crossing
     up through 50 (trend turning bullish), PE crossing down through 50.
     Trend-FOLLOWING hypothesis.
  4. ``rsi_reversal_vote`` -- RSI-14 crossing back above 30 from oversold
     (CE, bullish reversal) or back below 70 from overbought (PE, bearish
     reversal). Mean-REVERSION hypothesis -- deliberately different from
     (3), reported separately per this study's brief.
  5. ``confluence_vote`` -- a hard AND-gate: fires a direction at bar i only
     if bar i is itself a firing bar for at least one input signal AND at
     least ``min_agree`` DISTINCT input signals (by name) fired that SAME
     direction at some bar within the causal window ``[i-window_bars, i]``
     of the SAME trade_date. Structurally different from the already-tested
     weighted-SUM composite (``conditional_direction.composite_vote``) --
     this is "N independent signals must each confirm", not "a blended
     score crosses a margin". A bar where both directions separately clear
     ``min_agree`` within the same window abstains (ambiguous), matching
     this project's existing veto-on-conflict convention (e.g.
     ``conditional_direction.vote_or_trap``).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from snapshot_app.core.feature_engine import _atr as _fe_atr
from snapshot_app.core.feature_engine import _ema as _fe_ema
from snapshot_app.core.feature_engine import _rsi as _fe_rsi

CE = "CE"
PE = "PE"

DAY_COL = "trade_date"

RSI_PERIOD = 14
RSI_TREND_LEVEL = 50.0
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ADX_PERIOD = 14

BOLLINGER_WINDOW = 20
BOLLINGER_NUM_STD = 2.0
BOLLINGER_MIN_PERIODS = 5

SUPERTREND_ATR_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0

DEFAULT_CONFLUENCE_MIN_AGREE = 2
DEFAULT_CONFLUENCE_WINDOW_BARS = 2

FULL_DAY_FRAME_COLUMNS: List[str] = [
    "trade_date", "timestamp", "bar_index",
    "fut_close", "fut_high", "fut_low",
    "ema_9", "ema_21", "ema_50",
    # Already-live-computed multi-timeframe fields, read verbatim from each
    # snapshot's own `mtf_derived` block (see module docstring) -- NOT
    # recomputed. 15-minute fields are deliberately excluded (structurally
    # near-dead within a single reset-per-day session -- see docstring).
    "mtf_rsi_14_5m", "mtf_ema_9_5m", "mtf_ema_21_5m",
    "mtf_macd_line_5m", "mtf_macd_signal_5m", "mtf_bb_pct_b_5m",
]


# ══════════════════════════════════════════════════════════════════════════
# Full-day raw frame extraction (mirrors entry_quality_direction
# .build_raw_signal_frame's per-day-from-snapshots pattern).
# ══════════════════════════════════════════════════════════════════════════

def extract_full_day_row(snap, *, bar_index: int) -> Dict[str, object]:
    """One row of raw fields for a single snapshot, positioned by its
    0-based ``bar_index`` within the day (matching ``read_day_snapshots``'
    sort-by-timestamp contract -- bar_index 0 is the session's first bar,
    09:15). This is what lets the delayed-action stress test later re-locate
    the exact same bar inside a freshly re-read day's snapshot list, exactly
    as ``entry_quality_direction.compute_payoff_at_delay`` already assumes.

    ``mtf_*`` fields are read straight from the snapshot's own
    ``mtf_derived`` payload block (``market_snapshot.py``'s already-live
    multi-timeframe indicators -- see module docstring), not recomputed;
    missing/absent keys degrade to None, never a crash (matches this
    project's general "missing input -> NaN, never raise" convention)."""
    mtf = snap.raw_payload.get("mtf_derived") or {}
    return {
        "trade_date": snap.trade_date,
        "timestamp": snap.raw_payload.get("timestamp"),
        "bar_index": bar_index,
        "fut_close": snap.fut_close,
        "fut_high": snap.fut_high,
        "fut_low": snap.fut_low,
        "ema_9": snap.ema_9,
        "ema_21": snap.ema_21,
        "ema_50": snap.ema_50,
        "mtf_rsi_14_5m": mtf.get("rsi_14_5m"),
        "mtf_ema_9_5m": mtf.get("ema_9_5m"),
        "mtf_ema_21_5m": mtf.get("ema_21_5m"),
        "mtf_macd_line_5m": mtf.get("macd_line_5m"),
        "mtf_macd_signal_5m": mtf.get("macd_signal_5m"),
        "mtf_bb_pct_b_5m": mtf.get("bb_pct_b_5m"),
    }


def build_full_day_frame(snaps: Sequence) -> pd.DataFrame:
    """Full-day (bar_index 0 onward) raw-field frame for ONE chronological
    day of snapshots -- the input every indicator/crossover function in this
    module expects (see module docstring re: truncation/warm-up bias)."""
    if not snaps:
        return pd.DataFrame(columns=FULL_DAY_FRAME_COLUMNS)
    rows = [extract_full_day_row(s, bar_index=i) for i, s in enumerate(snaps)]
    return pd.DataFrame(rows)[FULL_DAY_FRAME_COLUMNS]


# ══════════════════════════════════════════════════════════════════════════
# Fresh indicators: RSI-14 (feature_engine-verbatim) and MACD (new).
# Both take a MULTI-DAY frame already sorted by (trade_date, bar_index) and
# reset internally per trade_date via groupby-transform -- callers do not
# need to loop by day themselves.
# ══════════════════════════════════════════════════════════════════════════

def compute_rsi_14(
    df: pd.DataFrame, *, close_col: str = "fut_close", day_col: str = DAY_COL,
) -> pd.Series:
    """Wilder's RSI-14, reset every trade_date -- reuses
    ``feature_engine._rsi`` verbatim (the same formula that produces the
    live ``osc_rsi_14`` feature, never given a training-view column -- see
    module docstring) so this is bit-identical to the live serving
    definition, not a re-derivation. ``df`` must be sorted by
    ``(day_col, bar_index)`` ascending within each day and should carry a
    FULL day's bars (bar_index 0 onward) to avoid EMA warm-up bias."""
    close = pd.to_numeric(df[close_col], errors="coerce")
    return close.groupby(df[day_col], sort=False).transform(lambda s: _fe_rsi(s, RSI_PERIOD))


def compute_macd(
    df: pd.DataFrame,
    *,
    close_col: str = "fut_close",
    day_col: str = DAY_COL,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> pd.DataFrame:
    """Standard MACD (fast-EMA minus slow-EMA of close, plus an EMA-smoothed
    signal line of that difference), reset every trade_date at BOTH stages
    (the line's own EMAs, and the signal line's EMA of the line) -- "reset
    each day, don't carry state across days" per this study's brief. Reuses
    ``feature_engine._ema`` (the same primitive ``ema_9``/``ema_21``/
    ``ema_50`` are built from) rather than a third-party TA library. Returns
    a 3-column frame (``macd_line``, ``macd_signal``, ``macd_hist``) aligned
    to ``df``'s index."""
    close = pd.to_numeric(df[close_col], errors="coerce")
    day = df[day_col]
    macd_line = close.groupby(day, sort=False).transform(lambda s: _fe_ema(s, fast) - _fe_ema(s, slow))
    macd_signal = macd_line.groupby(day, sort=False).transform(lambda s: _fe_ema(s, signal))
    macd_hist = macd_line - macd_signal
    return pd.DataFrame(
        {"macd_line": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist}, index=df.index,
    )


# ══════════════════════════════════════════════════════════════════════════
# Day-boundary-safe crossover / level-cross primitives.
# ══════════════════════════════════════════════════════════════════════════

def _same_day_mask(day: pd.Series) -> pd.Series:
    """True where this row's trade_date equals the PRECEDING row's -- False
    (never True) for the first row of every trade_date, and for the very
    first row overall. ``day`` must already be sorted ascending by
    ``(trade_date, bar_index)``."""
    return day.eq(day.shift(1))


def detect_cross(fast: pd.Series, slow: pd.Series, *, day: pd.Series) -> pd.Series:
    """CE at the bar ``fast`` crosses from <= ``slow`` to > ``slow``; PE at
    the bar it crosses from >= ``slow`` to < ``slow``; None elsewhere --
    INCLUDING the first bar of every trade_date (no valid previous-bar state
    within that day -- see module docstring's day-boundary discipline).
    Fires ONLY at the exact crossing bar, never for every bar of the
    resulting spread state (that would be the already-tested static-spread-
    sign feature, e.g. ``ema_above_21``)."""
    f = pd.to_numeric(fast, errors="coerce")
    s = pd.to_numeric(slow, errors="coerce")
    diff = f - s
    same_day = _same_day_mask(day)
    prev_diff = diff.shift(1).where(same_day)
    up = (prev_diff <= 0) & (diff > 0)
    down = (prev_diff >= 0) & (diff < 0)
    out = np.where(up.fillna(False).to_numpy(), CE, np.where(down.fillna(False).to_numpy(), PE, None))
    return pd.Series(out, index=fast.index if isinstance(fast, pd.Series) else diff.index, dtype=object)


def ema_cross_vote(
    df: pd.DataFrame, *, fast_col: str, slow_col: str, day_col: str = DAY_COL,
) -> pd.Series:
    """EMA golden-cross ("fast crosses above slow" -> CE) / death-cross
    ("fast crosses below slow" -> PE) -- e.g. ``fast_col="ema_9",
    slow_col="ema_21"`` or ``fast_col="ema_21", slow_col="ema_50"``. NOT the
    static spread sign (``ema_order``/``ema_above_21``), which is a
    different, already-implicitly-tested feature (see module docstring)."""
    return detect_cross(df[fast_col], df[slow_col], day=df[day_col])


def macd_cross_vote(
    df: pd.DataFrame,
    *,
    day_col: str = DAY_COL,
    macd_line_col: str = "macd_line",
    macd_signal_col: str = "macd_signal",
) -> pd.Series:
    """CE when the MACD line crosses above its own signal line, PE when it
    crosses below."""
    return detect_cross(df[macd_line_col], df[macd_signal_col], day=df[day_col])


def rsi_trend_cross_vote(
    df: pd.DataFrame, *, rsi_col: str = "rsi_14", day_col: str = DAY_COL, level: float = RSI_TREND_LEVEL,
) -> pd.Series:
    """Trend-FOLLOWING read: CE the bar RSI-14 crosses UP through ``level``
    (default 50), PE the bar it crosses DOWN through it. A pure level-cross
    is exactly ``detect_cross(rsi, constant(level))``."""
    level_series = pd.Series(level, index=df.index)
    return detect_cross(df[rsi_col], level_series, day=df[day_col])


def rsi_reversal_vote(
    df: pd.DataFrame,
    *,
    rsi_col: str = "rsi_14",
    day_col: str = DAY_COL,
    oversold: float = RSI_OVERSOLD,
    overbought: float = RSI_OVERBOUGHT,
) -> pd.Series:
    """Mean-REVERSION read (a DIFFERENT hypothesis from
    ``rsi_trend_cross_vote`` -- reported separately per this study's brief):
    CE the bar RSI-14 crosses back ABOVE ``oversold`` (default 30) having
    been below it the prior bar (bullish bounce); PE the bar it crosses back
    BELOW ``overbought`` (default 70) having been above it the prior bar
    (bearish fade). Day-boundary-safe via the same ``same_day`` mask as
    ``detect_cross`` -- the first bar of every trade_date cannot fire."""
    rsi = pd.to_numeric(df[rsi_col], errors="coerce")
    day = df[day_col]
    same_day = _same_day_mask(day)
    prev_rsi = rsi.shift(1).where(same_day)
    bullish = (prev_rsi < oversold) & (rsi >= oversold)
    bearish = (prev_rsi > overbought) & (rsi <= overbought)
    out = np.where(bullish.fillna(False).to_numpy(), CE, np.where(bearish.fillna(False).to_numpy(), PE, None))
    return pd.Series(out, index=df.index, dtype=object)


# ══════════════════════════════════════════════════════════════════════════
# Confluence: hard AND-gate across independent crossover signals.
# ══════════════════════════════════════════════════════════════════════════

def confluence_vote(
    votes: Dict[str, pd.Series],
    *,
    day: pd.Series,
    min_agree: int = DEFAULT_CONFLUENCE_MIN_AGREE,
    window_bars: int = DEFAULT_CONFLUENCE_WINDOW_BARS,
) -> pd.Series:
    """Fires direction ``d`` at bar i only if:
      (a) bar i is ITSELF a firing bar (any direction) for at least one of
          the named signals in ``votes`` -- confluence anchors to a real
          fire, it does not invent a new fire bar of its own -- AND
      (b) at least ``min_agree`` DISTINCT signal names in ``votes`` fired
          direction ``d`` at SOME bar within the causal window
          ``[i-window_bars, i]`` of the SAME trade_date (never crossing a
          day boundary -- each day is processed independently).
    Abstains (None) if both directions separately clear ``min_agree`` within
    the same window (ambiguous), matching this project's existing
    veto-on-conflict convention.

    All series in ``votes`` and ``day`` must be positionally aligned (same
    length, same row order, already sorted by ``(trade_date, bar_index)``
    ascending) -- this is a POSITIONAL algorithm (uses ``.to_numpy()``
    throughout), not an index-based one.
    """
    if min_agree < 2:
        raise ValueError(f"confluence_vote: min_agree must be >= 2 (got {min_agree}) -- 1 signal is not a confluence")
    names = list(votes.keys())
    day_arr = np.asarray(day)
    n_rows = len(day_arr)
    ce_fired = {name: (np.asarray(votes[name]) == CE) for name in names}
    pe_fired = {name: (np.asarray(votes[name]) == PE) for name in names}
    out = np.full(n_rows, None, dtype=object)

    start = 0
    while start < n_rows:
        end = start
        while end < n_rows and day_arr[end] == day_arr[start]:
            end += 1
        for i in range(start, end):
            lo = max(start, i - window_bars)
            anchor_fires = any(ce_fired[name][i] or pe_fired[name][i] for name in names)
            if not anchor_fires:
                continue
            ce_count = sum(1 for name in names if ce_fired[name][lo:i + 1].any())
            pe_count = sum(1 for name in names if pe_fired[name][lo:i + 1].any())
            ce_ok = ce_count >= min_agree
            pe_ok = pe_count >= min_agree
            if ce_ok and not pe_ok:
                out[i] = CE
            elif pe_ok and not ce_ok:
                out[i] = PE
        start = end

    index = votes[names[0]].index if names and isinstance(votes[names[0]], pd.Series) else None
    return pd.Series(out, index=index, dtype=object)


# ══════════════════════════════════════════════════════════════════════════
# Optional / "if time permits" family: ADX +DI/-DI crossover, Bollinger Band
# breakout, Supertrend flip. Each reuses feature_engine primitives where they
# already exist (ATR) and reimplements verbatim where the live pipeline
# computes the base indicator but never exposes the specific sub-component
# this study needs (ADX's own internal +DI/-DI; Bollinger's bb_upper/
# bb_lower, present in feature_engine.py's SCHEMA but never given a
# training-view column, same situation as RSI-14 -- see module docstring).
# ══════════════════════════════════════════════════════════════════════════

def _adx_di_single_day(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ADX_PERIOD) -> pd.DataFrame:
    """Verbatim reproduction of ``feature_engine._adx``'s internal TR/DM/
    smoothing arithmetic for ONE trading day, additionally exposing +DI/-DI
    (the live pipeline only ever returns the final smoothed ADX value,
    `adx_14` -- these two intermediate directional-index series are computed
    internally but never returned or given a training-view column)."""
    prev_close = close.shift(1)
    up_move = high.diff()
    dn_move = -low.diff()
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0.0), 0.0).fillna(0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0.0), 0.0).fillna(0.0)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1,
    ).max(axis=1).fillna(0.0)
    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False, min_periods=1).mean()
    safe_atr = atr.replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=1).mean() / safe_atr
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=1).mean() / safe_atr
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=1).mean()
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx_14_fresh": adx}, index=close.index)


def compute_di_and_adx(
    df: pd.DataFrame,
    *,
    high_col: str = "fut_high",
    low_col: str = "fut_low",
    close_col: str = "fut_close",
    day_col: str = DAY_COL,
    period: int = ADX_PERIOD,
) -> pd.DataFrame:
    """+DI/-DI/ADX-14, reset every trade_date -- see ``_adx_di_single_day``.
    ``df`` must be sorted by ``(day_col, bar_index)`` ascending."""
    parts = [
        _adx_di_single_day(g[high_col], g[low_col], g[close_col], period)
        for _, g in df.groupby(day_col, sort=False)
    ]
    return pd.concat(parts).reindex(df.index)


def adx_di_cross_vote(
    df: pd.DataFrame,
    *,
    day_col: str = DAY_COL,
    plus_di_col: str = "plus_di",
    minus_di_col: str = "minus_di",
    adx_col: str = "adx_14_fresh",
    min_adx: Optional[float] = None,
) -> pd.Series:
    """CE the bar +DI crosses above -DI, PE the bar it crosses below --
    the classic ADX directional-index crossover. If ``min_adx`` is given,
    suppresses fires where the concurrent ADX-14 is below it (a common
    discretionary-trader filter: ignore a DI cross while the trend-strength
    reading itself says there's no real trend to follow)."""
    votes = detect_cross(df[plus_di_col], df[minus_di_col], day=df[day_col])
    if min_adx is None:
        return votes
    adx = pd.to_numeric(df[adx_col], errors="coerce")
    strong = (adx >= min_adx).fillna(False).to_numpy()
    out = np.where(strong, votes.to_numpy(), None)
    return pd.Series(out, index=votes.index, dtype=object)


def compute_bollinger_bands(
    df: pd.DataFrame,
    *,
    close_col: str = "fut_close",
    day_col: str = DAY_COL,
    window: int = BOLLINGER_WINDOW,
    num_std: float = BOLLINGER_NUM_STD,
    min_periods: int = BOLLINGER_MIN_PERIODS,
) -> pd.DataFrame:
    """20-bar Bollinger mid/upper/lower bands, reset every trade_date --
    same rolling-window convention ``feature_engine._layer_2_technicals``
    uses for ``bb_upper``/``bb_lower`` (present in that module's own SCHEMA
    but never given a training-view column -- the training view only
    carries the derived ``bb_width_20``, the same situation as RSI-14; see
    module docstring)."""
    close = pd.to_numeric(df[close_col], errors="coerce")
    grouped = close.groupby(df[day_col], sort=False)
    bb_mid = grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).mean())
    bb_std = grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).std())
    bb_upper = bb_mid + num_std * bb_std
    bb_lower = bb_mid - num_std * bb_std
    return pd.DataFrame({"bb_mid": bb_mid, "bb_upper": bb_upper, "bb_lower": bb_lower}, index=df.index)


def bollinger_breakout_vote(
    df: pd.DataFrame,
    *,
    close_col: str = "fut_close",
    day_col: str = DAY_COL,
    upper_col: str = "bb_upper",
    lower_col: str = "bb_lower",
) -> pd.Series:
    """CE the bar close crosses ABOVE the upper band (bullish breakout), PE
    the bar it crosses BELOW the lower band (bearish breakout). Day-boundary-
    safe via the same ``same_day`` mask as ``detect_cross``."""
    close = pd.to_numeric(df[close_col], errors="coerce")
    upper = pd.to_numeric(df[upper_col], errors="coerce")
    lower = pd.to_numeric(df[lower_col], errors="coerce")
    same_day = _same_day_mask(df[day_col])
    prev_close = close.shift(1).where(same_day)
    prev_upper = upper.shift(1).where(same_day)
    prev_lower = lower.shift(1).where(same_day)
    up = (prev_close <= prev_upper) & (close > upper)
    down = (prev_close >= prev_lower) & (close < lower)
    out = np.where(up.fillna(False).to_numpy(), CE, np.where(down.fillna(False).to_numpy(), PE, None))
    return pd.Series(out, index=df.index, dtype=object)


def pctb_breakout_vote(
    df: pd.DataFrame,
    *,
    pctb_col: str,
    day_col: str = DAY_COL,
    upper_level: float = 1.0,
    lower_level: float = 0.0,
) -> pd.Series:
    """Bollinger %B breakout using an ALREADY-COMPUTED %B series (e.g.
    ``mtf_derived.bb_pct_b_5m`` -- %B = (close-lower)/(upper-lower), so
    %B>1 means price is above the upper band, %B<0 means below the lower
    band): CE the bar %B crosses UP through ``upper_level`` (default 1.0,
    bullish breakout), PE the bar it crosses DOWN through ``lower_level``
    (default 0.0, bearish breakout). Day-boundary-safe via the same
    ``same_day`` mask as ``detect_cross``/``rsi_reversal_vote``."""
    pctb = pd.to_numeric(df[pctb_col], errors="coerce")
    same_day = _same_day_mask(df[day_col])
    prev_pctb = pctb.shift(1).where(same_day)
    up = (prev_pctb <= upper_level) & (pctb > upper_level)
    down = (prev_pctb >= lower_level) & (pctb < lower_level)
    out = np.where(up.fillna(False).to_numpy(), CE, np.where(down.fillna(False).to_numpy(), PE, None))
    return pd.Series(out, index=df.index, dtype=object)


def _supertrend_single_day(
    high: pd.Series, low: pd.Series, close: pd.Series,
    *, period: int = SUPERTREND_ATR_PERIOD, multiplier: float = SUPERTREND_MULTIPLIER,
) -> pd.DataFrame:
    """Standard ATR-based Supertrend (the widely-cited default ATR(10) /
    multiplier(3.0) parameterization -- the ratcheting final-band and
    trend-flip rules below are the textbook formula, not a bespoke variant)
    for ONE trading day. Reuses ``feature_engine._atr`` verbatim for the ATR
    leg. Bar 0 of each day initializes the bands fresh (trend defaults to
    uptrend) -- there is no meaningful "previous day's trend" to carry in,
    matching this module's day-reset convention throughout."""
    n = len(close)
    atr = _fe_atr(high, low, close, period)
    hl2 = (high + low) / 2.0
    basic_upper = (hl2 + multiplier * atr).to_numpy()
    basic_lower = (hl2 - multiplier * atr).to_numpy()
    close_arr = pd.to_numeric(close, errors="coerce").to_numpy()

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)

    for i in range(n):
        if i == 0:
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            trend[i] = 1
            continue
        final_upper[i] = (
            basic_upper[i]
            if (basic_upper[i] < final_upper[i - 1] or close_arr[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower[i]
            if (basic_lower[i] > final_lower[i - 1] or close_arr[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )
        if trend[i - 1] == 1:
            trend[i] = -1 if close_arr[i] < final_lower[i] else 1
        else:
            trend[i] = 1 if close_arr[i] > final_upper[i] else -1

    supertrend_line = np.where(trend == 1, final_lower, final_upper)
    return pd.DataFrame(
        {"supertrend_line": supertrend_line, "supertrend_trend": trend}, index=close.index,
    )


def compute_supertrend(
    df: pd.DataFrame,
    *,
    high_col: str = "fut_high",
    low_col: str = "fut_low",
    close_col: str = "fut_close",
    day_col: str = DAY_COL,
    period: int = SUPERTREND_ATR_PERIOD,
    multiplier: float = SUPERTREND_MULTIPLIER,
) -> pd.DataFrame:
    """Supertrend line + trend direction (+1/-1), reset every trade_date --
    see ``_supertrend_single_day``. ``df`` must be sorted by
    ``(day_col, bar_index)`` ascending."""
    parts = [
        _supertrend_single_day(g[high_col], g[low_col], g[close_col], period=period, multiplier=multiplier)
        for _, g in df.groupby(day_col, sort=False)
    ]
    return pd.concat(parts).reindex(df.index)


def supertrend_flip_vote(
    df: pd.DataFrame, *, day_col: str = DAY_COL, trend_col: str = "supertrend_trend",
) -> pd.Series:
    """CE the bar Supertrend flips from downtrend(-1) to uptrend(+1); PE the
    bar it flips from uptrend to downtrend. A day's own first bar is a fresh
    initialization (``_supertrend_single_day`` always starts trend=+1), not
    a flip -- day-boundary-safe via the same ``same_day`` mask as
    ``detect_cross``."""
    trend = pd.to_numeric(df[trend_col], errors="coerce")
    same_day = _same_day_mask(df[day_col])
    prev_trend = trend.shift(1).where(same_day)
    up_flip = (prev_trend < 0) & (trend > 0)
    down_flip = (prev_trend > 0) & (trend < 0)
    out = np.where(up_flip.fillna(False).to_numpy(), CE, np.where(down_flip.fillna(False).to_numpy(), PE, None))
    return pd.Series(out, index=df.index, dtype=object)


__all__ = [
    "CE", "PE", "DAY_COL",
    "RSI_PERIOD", "RSI_TREND_LEVEL", "RSI_OVERSOLD", "RSI_OVERBOUGHT",
    "MACD_FAST", "MACD_SLOW", "MACD_SIGNAL",
    "ADX_PERIOD", "BOLLINGER_WINDOW", "BOLLINGER_NUM_STD", "BOLLINGER_MIN_PERIODS",
    "SUPERTREND_ATR_PERIOD", "SUPERTREND_MULTIPLIER",
    "DEFAULT_CONFLUENCE_MIN_AGREE", "DEFAULT_CONFLUENCE_WINDOW_BARS",
    "FULL_DAY_FRAME_COLUMNS",
    "extract_full_day_row", "build_full_day_frame",
    "compute_rsi_14", "compute_macd",
    "detect_cross", "ema_cross_vote", "macd_cross_vote",
    "rsi_trend_cross_vote", "rsi_reversal_vote",
    "confluence_vote",
    "compute_di_and_adx", "adx_di_cross_vote",
    "compute_bollinger_bands", "bollinger_breakout_vote", "pctb_breakout_vote",
    "compute_supertrend", "supertrend_flip_vote",
]

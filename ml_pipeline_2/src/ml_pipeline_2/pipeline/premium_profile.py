"""Option-premium level & intraday move-size profile check (BankNifty
research, 2026-09-27).

Motivation: the asymmetric stop/trail exit study (``variable_hold_exit.py``,
``ml_pipeline_2.scripts.study_asymmetric_exit_coinflip``) was built and
validated against NIFTY using a fixed PERCENTAGE grid
(``DEFAULT_STOP_GRID``/``DEFAULT_TRAIL_ACTIVATE_GRID``/
``DEFAULT_TRAIL_DISTANCE_GRID``, all expressed as a fraction of the entry
premium, e.g. "exit if premium falls 10% below entry"). Percentage returns
are already index-level-agnostic in the sense that a 10% stop means the
same thing whether entry premium is Rs.80 or Rs.180 -- but that does NOT
automatically mean the SAME percentage grid is equally well-scaled for a
different instrument. If BankNifty's real intraday premium moves are
typically much smaller (in %) than NIFTY's, most of the grid's stop/trail
levels would rarely trigger at all (every combination degenerating to a
time-cap exit, wasting the whole grid on a single effective policy); if
they are typically much larger, the tightest stop levels might trigger on
essentially every single trade before any real move develops. Before
running the full walk-forward study on BankNifty with NIFTY's exact grid
values, this module answers that empirically: what fraction of REAL
BankNifty ATM CE/PE forward premium paths ever reach each candidate
stop/activate level, and what does the underlying max-favorable /
max-adverse move distribution actually look like.

Two pure, instrument-agnostic aggregation functions live here --
deliberately generic (not BankNifty-specific logic), reusable for any
future instrument's version of this same sanity check. Only the owning
script (``ml_pipeline_2.scripts.check_option_premium_profile``) is run
with BankNifty-specific arguments for this task.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

import numpy as np


def path_extremes(entry_premium: float, premium_path: Sequence[float]) -> Dict[str, float]:
    """max_gain_pct / max_drawdown_pct reached anywhere along one entry's
    forward premium path, using the SAME sign/normalization convention as
    ``variable_hold_exit.simulate_exit``'s own ``drawdown`` computation
    (``(price - entry) / entry``) -- so the outputs here are directly
    comparable to that module's ``stop_loss_pct``/``trail_activate_pct``
    grid values. ``max_drawdown_pct`` is <= 0 (a pure gain-only path has
    drawdown 0.0, its lowest point being the entry price itself, never
    positive). Pure -- no snapshot/parquet dependency.

    Raises ValueError on an empty path or non-positive entry_premium (the
    same "nothing to measure" / "invalid entry" cases
    ``variable_hold_exit.simulate_exit`` itself guards against).
    """
    path = np.asarray(premium_path, dtype=float)
    if path.size == 0:
        raise ValueError("path_extremes requires at least one forward bar in premium_path")
    if entry_premium <= 0:
        raise ValueError(f"entry_premium must be positive, got {entry_premium}")
    rel = (path - entry_premium) / entry_premium
    return {
        "max_gain_pct": float(np.max(rel)),
        "max_drawdown_pct": float(np.min(rel)),
    }


def summarize_extremes(
    rows: Sequence[Dict[str, float]],
    *,
    stop_grid: Sequence[float] = (),
    activate_grid: Sequence[float] = (),
) -> Dict[str, Any]:
    """Aggregate a sequence of ``path_extremes`` rows into percentiles of
    max_gain_pct / max_drawdown_pct, plus (when `stop_grid`/`activate_grid`
    are given) the FRACTION of entries whose max_drawdown_pct would have
    triggered each stop level, and whose max_gain_pct would have triggered
    each trailing-activation level -- the concrete numeric answer to "does
    this percentage grid make sense for this instrument's real premium
    moves." A `stop_hit_rate` near 0 for the loosest grid level means that
    level (and everything looser) is effectively a no-op (equivalent to no
    stop at all, wasting a grid dimension); a rate near 1 for the tightest
    level means most/every trade gets stopped out almost immediately
    regardless of the entry's real quality.

    Returns ``{"n": 0}`` for an empty `rows` -- callers must check `n`
    before trusting any other key.
    """
    n = len(rows)
    if n == 0:
        return {"n": 0}
    gains = np.asarray([r["max_gain_pct"] for r in rows], dtype=float)
    drawdowns = np.asarray([r["max_drawdown_pct"] for r in rows], dtype=float)

    def _pctiles(arr: np.ndarray) -> Dict[str, float]:
        return {f"p{p}": round(float(np.percentile(arr, p)), 4) for p in (10, 25, 50, 75, 90)}

    stop_hit_rates = {f"stop_{s:.2f}": round(float(np.mean(drawdowns <= -s)), 4) for s in stop_grid}
    activate_hit_rates = {f"activate_{a:.2f}": round(float(np.mean(gains >= a)), 4) for a in activate_grid}
    return {
        "n": n,
        "max_gain_pct": _pctiles(gains),
        "max_drawdown_pct": _pctiles(drawdowns),
        "mean_max_gain_pct": round(float(gains.mean()), 4),
        "mean_max_drawdown_pct": round(float(drawdowns.mean()), 4),
        "stop_hit_rates": stop_hit_rates,
        "activate_hit_rates": activate_hit_rates,
    }


def entry_premium_level_summary(entry_premiums: Sequence[float]) -> Dict[str, Any]:
    """Plain descriptive stats (rupee terms, not %) of ATM entry premium
    levels -- context only (e.g. "is BankNifty's ATM premium typically much
    higher/lower than NIFTY's own"), never itself a % move statistic. Drops
    None/non-positive values rather than raising (mirrors this pipeline's
    drop-don't-impute convention for genuinely missing quotes)."""
    arr = np.asarray([p for p in entry_premiums if p is not None and p > 0], dtype=float)
    n = int(arr.size)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean": round(float(arr.mean()), 2),
        "median": round(float(np.median(arr)), 2),
        "p10": round(float(np.percentile(arr, 10)), 2),
        "p90": round(float(np.percentile(arr, 90)), 2),
        "min": round(float(arr.min()), 2),
        "max": round(float(arr.max()), 2),
    }


__all__ = ["path_extremes", "summarize_extremes", "entry_premium_level_summary"]

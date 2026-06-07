"""Exit simulator — prove the giveback fix in the e2e (board B-4.1, Sprint 5).

Pure-math. Walks a trade's intra-window price path in OPTION-PREMIUM space (delta proxy)
and applies an exit policy bar-by-bar, returning the realised exit % of premium. This is
what lets the e2e MEASURE exits instead of assuming a static loss cap:

  - hard stop      : cut a loser at -hard_stop_pct (caps the wrong-side asymmetry)
  - MFE-giveback   : once a winner has run, exit if it gives back > giveback_frac of its peak
                     (the documented "hit +2% then gave it all back" failure)
  - time stop      : otherwise exit at the horizon close

Honesty: the option path is a delta+theta proxy off the underlying path (no real per-strike
premium ticks here), same provisional basis as cost_ev. It models the *shape* of the exit
dynamics (stop vs trail vs time), which is what we need to compare policies — not a fill.

DATA LIMITATION (probed 2026-06-07, `ops/research/_probe_chain_hl.py`): per-strike rows in
`phase1_market_snapshots` carry only **LTP (1-min close)** — `ce_high/ce_low/pe_high/pe_low`
are 0% populated (all NaN). So `simulate_exit_real` runs **1-min-close granularity** (the
held-strike path falls back high=low=close). Consequence: intrabar stop breaches that recover
by the close are MISSED (loser P&L slightly optimistic), and the trail uses close peaks (true
intrabar MFE >= shown). True intrabar fidelity needs a FORWARD ingestion change to persist
per-strike OHLC (or ticks) — it cannot be recovered from existing data.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitParams:
    premium_pts: float = 180.0          # entry ATM premium (pts == rupees/unit)
    delta: float = 0.5                  # ATM delta proxy (underlying pt -> premium pt)
    theta_pct_per_bar: float = 0.0006   # per-minute theta drag as % of premium
    hard_stop_pct: float = 0.045        # max loss cap on premium
    mfe_giveback_frac: float = 0.5      # exit if give back > this fraction of peak MFE
    min_mfe_to_trail: float = 0.02      # only start trailing once MFE exceeds this


def _prem_pct(delta: float, disp: float, premium_pts: float, theta: float, bar_idx: int) -> float:
    """Option premium return (% of premium) for a favourable-displacement `disp` (pt)."""
    return delta * disp / premium_pts - theta * (bar_idx + 1)


def walk_premium_path(
    prem_path: list[tuple[float, float, float]],
    params: ExitParams | None = None,
    *,
    time_stop_only: bool = False,
) -> dict[str, float | int | str]:
    """Apply the exit policy over a premium-% path: per-bar (best_pct, worst_pct, close_pct),
    each already a return on premium. Model-free core — used by both the delta proxy and the
    REAL per-strike option path. hard-stop on the adverse extreme, MFE-giveback trail, time-stop.
    """
    p = params or ExitParams()
    if not prem_path:
        return {"exit_pct": 0.0, "exit_bar": 0, "reason": "no_path"}
    peak_mfe = -1e9
    for t, (best_pct, worst_pct, close_pct) in enumerate(prem_path):
        if not time_stop_only:
            if worst_pct <= -p.hard_stop_pct:
                return {"exit_pct": -p.hard_stop_pct, "exit_bar": t, "reason": "hard_stop"}
            peak_mfe = max(peak_mfe, best_pct)
            if peak_mfe >= p.min_mfe_to_trail and close_pct <= peak_mfe * (1.0 - p.mfe_giveback_frac):
                return {"exit_pct": round(close_pct, 5), "exit_bar": t, "reason": "mfe_giveback"}
        else:
            peak_mfe = max(peak_mfe, best_pct)
    return {"exit_pct": round(prem_path[-1][2], 5), "exit_bar": len(prem_path) - 1, "reason": "time_stop"}


def simulate_exit(
    side: str,
    future_path: list[tuple[float, float, float]],
    params: ExitParams | None = None,
    *,
    time_stop_only: bool = False,
) -> dict[str, float | int | str]:
    """Return the realised exit for one trade.

    ``side`` is "CE" or "PE"; ``future_path`` is per-bar (high_disp, low_disp, close_disp)
    underlying displacement from entry (pt). ``time_stop_only=True`` disables stop+trail
    (baseline = hold to horizon) so we can A/B the giveback fix.
    """
    p = params or ExitParams()
    if not future_path:
        return {"exit_pct": 0.0, "exit_bar": 0, "reason": "no_path"}
    ce = side == "CE"
    prem_path = []
    for t, (high_disp, low_disp, close_disp) in enumerate(future_path):
        fav_best = high_disp if ce else -low_disp     # most favourable point in the bar
        fav_worst = low_disp if ce else -high_disp     # most adverse point in the bar
        prem_path.append((
            _prem_pct(p.delta, fav_best, p.premium_pts, p.theta_pct_per_bar, t),
            _prem_pct(p.delta, fav_worst, p.premium_pts, p.theta_pct_per_bar, t),
            _prem_pct(p.delta, (close_disp if ce else -close_disp), p.premium_pts, p.theta_pct_per_bar, t),
        ))
    return walk_premium_path(prem_path, p, time_stop_only=time_stop_only)


def simulate_exit_real(
    prem_path: list[tuple[float, float, float]],
    params: ExitParams | None = None,
    *,
    time_stop_only: bool = False,
) -> dict[str, float | int | str]:
    """Exit on the REAL held-strike option path: per-bar (best_pct, worst_pct, close_pct)
    measured from the actual chain ltp/high/low vs the entry premium (no delta/theta proxy —
    IV crush + gamma + decay are already in the prices)."""
    return walk_premium_path(prem_path, params, time_stop_only=time_stop_only)


__all__ = ["ExitParams", "simulate_exit", "simulate_exit_real", "walk_premium_path"]

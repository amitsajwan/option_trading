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

    peak_mfe = -1e9
    for t, (high_disp, low_disp, close_disp) in enumerate(future_path):
        # favourable / adverse underlying displacement for this side
        fav_best = high_disp if ce else -low_disp     # most favourable point in the bar
        fav_worst = low_disp if ce else -high_disp     # most adverse point in the bar
        best_pct = _prem_pct(p.delta, fav_best, p.premium_pts, p.theta_pct_per_bar, t)
        worst_pct = _prem_pct(p.delta, fav_worst, p.premium_pts, p.theta_pct_per_bar, t)
        close_pct = _prem_pct(p.delta, (close_disp if ce else -close_disp), p.premium_pts, p.theta_pct_per_bar, t)

        if not time_stop_only:
            # 1) hard stop — adverse extreme breaches the cap (conservative: fill at the cap)
            if worst_pct <= -p.hard_stop_pct:
                return {"exit_pct": -p.hard_stop_pct, "exit_bar": t, "reason": "hard_stop"}
            # 2) MFE giveback — once we've run, don't hand it back
            peak_mfe = max(peak_mfe, best_pct)
            if peak_mfe >= p.min_mfe_to_trail and close_pct <= peak_mfe * (1.0 - p.mfe_giveback_frac):
                return {"exit_pct": round(close_pct, 5), "exit_bar": t, "reason": "mfe_giveback"}
        else:
            peak_mfe = max(peak_mfe, best_pct)

    # 3) time stop — exit at the last bar's close
    last_close_disp = future_path[-1][2]
    exit_pct = _prem_pct(p.delta, (last_close_disp if ce else -last_close_disp),
                         p.premium_pts, p.theta_pct_per_bar, len(future_path) - 1)
    return {"exit_pct": round(exit_pct, 5), "exit_bar": len(future_path) - 1, "reason": "time_stop"}


__all__ = ["ExitParams", "simulate_exit"]

"""Cost-aware, direction-agnostic option-payoff labels (NIFTY entry-focus
research, 2026-09-26).

The existing entry label (``train_entry_dhan_v3.add_labels``) is direction-
agnostic but measures raw *index-point* movement: ``max(|up_move|,
|down_move|) >= threshold``. That says nothing about whether the move would
actually have been a profitable OPTION trade after IV/theta/cost effects.
This module answers the sharper question directly: "if we'd bought whichever
side (CE or PE) turned out to be right, at this bar's own ATM strike, would
it have cleared cost by the time we exited?" -- a graded, continuous score
rather than a binary flag, matching the "50/100 points" framing (a bar isn't
just entry-or-not, it has a magnitude of missed/caught opportunity).

Design notes carried over from a real prior attempt at this exact idea
(BankNifty, Kite-era, 2026-05-17 -- see
``docs/archive/handovers_status/PROJECT_PLAN.md`` sec 16-18 and
``ml_pipeline_2/docs/training/OPTION_LABEL_CONTRACT.md``; its labeler module
was deleted in the 2026-09-09 dead-code cleanup, but its lessons weren't
wrong, just built against a since-migrated Kite data schema):
  - Liquidity filter (contract clause #9): reject a leg with OI/premium too
    thin to have actually been fillable. Reused verbatim (same thresholds)
    rather than re-derived.
  - Fixed-contract exit (contract clause #1/#5): price the SAME strike at
    exit that was bought at entry, never re-based to a later ATM -- spot can
    drift across a strike step well within a 15-minute NIFTY hold.
  - Missing quote -> skip the label, never impute (contract clause #10).
  - The prior attempt's flagship recipe looked strong on one holdout window
    and flipped to NO_EDGE on another -- a single train/holdout split is not
    enough evidence. Callers of this module should evaluate across multiple
    rolling windows (see ``ml_pipeline_2/scripts/pipeline_backtest.py``
    conventions), not just one.

Cost is sourced from the exact class live trading already uses
(``strategy_app.senses.cost_ev.CostEvSense``, itself built on
``strategy_app.cost_model.TradingCostModel``) -- never re-derived here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from strategy_app.cost_model import TradingCostModel
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.senses.cost_ev import CostEvSense

# ml_pipeline_2/docs/training/OPTION_LABEL_CONTRACT.md clause #9 (2026-05-17).
# Reused verbatim: a leg this thin/illiquid would never actually be fillable
# at the modeled price, so labeling on it would teach the model a fantasy.
MIN_ENTRY_OI = 1000.0
MIN_ENTRY_PREMIUM = 5.0


@dataclass(frozen=True)
class LegPayoff:
    option_type: str  # "CE" or "PE"
    entry_premium: float
    exit_premium: float
    entry_oi: Optional[float]
    cost_pct: float
    net_return: float  # (exit-entry)/entry - cost_pct, i.e. after round-trip cost


@dataclass(frozen=True)
class BarPayoff:
    strike: int
    ce: Optional[LegPayoff]
    pe: Optional[LegPayoff]

    @property
    def payoff_score(self) -> Optional[float]:
        """max(net CE return, net PE return) across whichever leg(s) had a
        usable quote -- None if neither did (skip, not impute)."""
        candidates = [leg.net_return for leg in (self.ce, self.pe) if leg is not None]
        return max(candidates) if candidates else None

    @property
    def winning_side(self) -> Optional[str]:
        """Which leg produced payoff_score -- None if payoff_score is None."""
        best: Optional[LegPayoff] = None
        for leg in (self.ce, self.pe):
            if leg is None:
                continue
            if best is None or leg.net_return > best.net_return:
                best = leg
        return best.option_type if best is not None else None


def _leg_payoff(
    option_type: str,
    *,
    entry_premium: Optional[float],
    exit_premium: Optional[float],
    entry_oi: Optional[float],
    lot_size: int,
    cost_model: TradingCostModel,
) -> Optional[LegPayoff]:
    if entry_premium is None or exit_premium is None:
        return None
    if entry_premium < MIN_ENTRY_PREMIUM or entry_premium <= 0:
        return None
    if entry_oi is not None and entry_oi < MIN_ENTRY_OI:
        return None
    cost_pct = CostEvSense(
        premium_pts=entry_premium, lot_qty=lot_size, cost_model=cost_model
    ).cost_pct()
    net_return = (exit_premium - entry_premium) / entry_premium - cost_pct
    return LegPayoff(option_type, entry_premium, exit_premium, entry_oi, cost_pct, net_return)


def compute_bar_payoff(
    entry: SnapshotAccessor,
    exit_snap: SnapshotAccessor,
    *,
    lot_size: int,
    cost_model: Optional[TradingCostModel] = None,
) -> Optional[BarPayoff]:
    """Direction-agnostic, cost-aware payoff for holding from `entry` to
    `exit_snap`, bought at `entry`'s OWN ATM strike (fixed-contract -- never
    re-based to whatever is ATM at exit time). Returns None only if the
    entry bar itself has no resolvable ATM strike; a BarPayoff with both legs
    None (e.g. everything illiquid) is still returned so callers can
    distinguish "no strike at all" from "strike existed, no usable leg".
    """
    strike = entry.atm_strike
    if strike is None:
        return None
    cm = cost_model or TradingCostModel()
    ce = _leg_payoff(
        "CE",
        entry_premium=entry.atm_ce_close,
        exit_premium=exit_snap.option_ltp("CE", strike),
        entry_oi=entry.atm_ce_oi,
        lot_size=lot_size,
        cost_model=cm,
    )
    pe = _leg_payoff(
        "PE",
        entry_premium=entry.atm_pe_close,
        exit_premium=exit_snap.option_ltp("PE", strike),
        entry_oi=entry.atm_pe_oi,
        lot_size=lot_size,
        cost_model=cm,
    )
    return BarPayoff(strike=strike, ce=ce, pe=pe)


def label_day(
    snapshots: Sequence[SnapshotAccessor],
    *,
    horizon_bars: int,
    lot_size: int,
    session_start_min: int = 9 * 60 + 45,
    session_end_min: int = 15 * 60 + 5,
    cost_model: Optional[TradingCostModel] = None,
) -> list[Optional[BarPayoff]]:
    """One BarPayoff (or None) per bar in `snapshots`, assumed chronological
    and all from the same trade_date. `horizon_bars` is a bar-index offset,
    not wall-clock minutes -- caller must confirm the snapshot cadence
    matches what the horizon is meant to represent (NIFTY's canonical
    snapshots are ~1-minute bars, matching train_entry_dhan_v3's minute-based
    horizon_min convention).

    Bars outside the 9:45-15:05 IST session window, or within `horizon_bars`
    of session end (lookahead unavailable), get None -- mirrors
    train_entry_dhan_v3.add_labels' own session-filter/NaN-tail convention
    so this label is directly comparable to the existing index-move one.
    """
    n = len(snapshots)
    out: list[Optional[BarPayoff]] = [None] * n
    cm = cost_model or TradingCostModel()
    for i in range(n):
        entry = snapshots[i]
        ts = entry.timestamp
        if ts is None:
            continue
        minute_of_day = ts.hour * 60 + ts.minute
        if minute_of_day < session_start_min or minute_of_day > session_end_min:
            continue
        if i + horizon_bars >= n:
            continue
        out[i] = compute_bar_payoff(entry, snapshots[i + horizon_bars], lot_size=lot_size, cost_model=cm)
    return out


@dataclass(frozen=True)
class StrikeCoverageReport:
    """QA report for whether the historical option chain is complete enough
    to trust this label -- run BEFORE training on it. Mirrors
    OPTION_LABEL_CONTRACT.md's sanity-gate discipline (missing-quote rate
    <=30%) rather than inventing a new threshold for that half of the check.

    The positive-rate band is NOT reused from that contract, on purpose.
    OPTION_LABEL_CONTRACT.md's 5-60% band was calibrated for a SINGLE-LEG
    recipe label (e.g. "did buying ATM_PE_15 alone clear cost") -- a coin-
    flip-ish directional bet, where >60% positive would genuinely suggest
    leakage. This label is `max(net_ce, net_pe)` -- a best-of-two, oracle-
    style score that only needs ONE side to work. Since CE and PE returns
    move in roughly opposite directions for any real underlying move, "some
    leg was profitable net of cost" is structurally much more common than
    "the specific leg you'd have bought was profitable" -- a real run
    against 2024-11-2026-09 NIFTY history (2026-09-26) measured 82.9%
    positive, which is high but not evidence of a bug on its own. The gate
    here instead only flags the genuinely-alarming extremes: near-zero
    (nothing pays even with perfect hindsight -- the label or cost model is
    likely broken) or near-total (implausible even for an oracle -- suggests
    cost is being computed as ~zero or premiums are on the wrong scale).
    """

    total_entry_bars: int
    bars_with_atm_strike: int
    bars_with_any_leg: int
    bars_with_both_legs: int
    missing_quote_rate: float  # fraction of bars-with-a-strike that got NO usable leg
    payoff_positive_rate: float  # fraction of scored bars with payoff_score > 0 (pre ship-gate use)
    payoff_mean: Optional[float] = None
    payoff_median: Optional[float] = None
    payoff_p10: Optional[float] = None
    payoff_p90: Optional[float] = None

    @property
    def passes_missing_quote_gate(self) -> bool:
        return self.missing_quote_rate <= 0.30

    @property
    def passes_positive_rate_gate(self) -> bool:
        # See the class docstring: this is an oracle-style max(CE,PE) label,
        # not a single-leg recipe -- only the implausible extremes gate.
        return 0.02 <= self.payoff_positive_rate <= 0.98


def measure_strike_coverage(labels: Sequence[Optional[BarPayoff]]) -> StrikeCoverageReport:
    """Aggregate QA stats over one or more days' `label_day(...)` output.
    Callers should concatenate multiple days' lists before calling this for
    a representative sample -- a single day is too thin to judge coverage."""
    total = len(labels)
    with_strike = [bp for bp in labels if bp is not None]
    with_any_leg = [bp for bp in with_strike if bp.payoff_score is not None]
    with_both = [bp for bp in with_strike if bp.ce is not None and bp.pe is not None]
    missing_quote_rate = (
        1.0 - (len(with_any_leg) / len(with_strike)) if with_strike else 0.0
    )
    scores = sorted(bp.payoff_score for bp in with_any_leg if bp.payoff_score is not None)
    positive = [s for s in scores if s > 0]
    payoff_positive_rate = (len(positive) / len(scores)) if scores else 0.0

    def _pct(p: float) -> Optional[float]:
        if not scores:
            return None
        idx = min(len(scores) - 1, max(0, int(round(p * (len(scores) - 1)))))
        return scores[idx]

    payoff_mean = (sum(scores) / len(scores)) if scores else None
    return StrikeCoverageReport(
        total_entry_bars=total,
        bars_with_atm_strike=len(with_strike),
        bars_with_any_leg=len(with_any_leg),
        bars_with_both_legs=len(with_both),
        missing_quote_rate=missing_quote_rate,
        payoff_positive_rate=payoff_positive_rate,
        payoff_mean=round(payoff_mean, 5) if payoff_mean is not None else None,
        payoff_median=round(_pct(0.5), 5) if _pct(0.5) is not None else None,
        payoff_p10=round(_pct(0.10), 5) if _pct(0.10) is not None else None,
        payoff_p90=round(_pct(0.90), 5) if _pct(0.90) is not None else None,
    )

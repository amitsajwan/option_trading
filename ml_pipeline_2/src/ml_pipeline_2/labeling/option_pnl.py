"""Option-P&L labeler — per-recipe binary labels derived from realized option fills.

Per [OPTION_LABEL_CONTRACT.md](../../../../docs/training/OPTION_LABEL_CONTRACT.md):

  For every decision minute t0, for every recipe:
    1. Pick strike at t0 from snapshot.atm_strike + offset * strike_step
    2. Read entry premium at t0 close
    3. Walk t0+1 .. t0+max_hold, checking premium-relative stop/target on close ONLY
       (NOT intra-bar OHLC — runtime sees only LTP per snapshot, so labels must match)
    4. If neither stop nor target hit by t0+max_hold, exit at max_hold close
    5. Apply canonical TradingCostModel (Rs.20/order brokerage, 2.5 + 7.5 bps charges + slippage)
    6. label = 1 if net P&L > 0 else 0

  Skip with reason_code (no label emitted) on:
    - entry < SOFT_CLOSE (15:00 IST)? otherwise no entry
    - t0 + max_hold > HARD_CLOSE (15:15 IST)? otherwise no exit
    - missing premium at entry or any intermediate bar
    - entry_premium < min_entry_premium (untradeable)
    - entry_oi < min_oi (untradeable)

Pure functions only — no I/O. CLI driver (build_option_pnl_labels.py) handles
the parquet iteration. This module is tested in isolation against synthetic
input dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from strategy_app.constants import (
    HARD_CLOSE_MINUTE,
    SOFT_CLOSE_MINUTE,
    resolve_lot_size,
)
from strategy_app.cost_model import TradingCostModel


@dataclass(frozen=True)
class Recipe:
    """One labeling recipe: which strike, which direction, how long to hold."""

    id: str
    option_type: str  # "CE" | "PE"
    strike_offset_steps: int  # 0 = ATM, +1 = next OTM (for CE: higher strike; for PE: lower)
    max_hold_bars: int
    stop_pct_of_premium: float  # e.g. 0.20 = 20% premium drop triggers stop
    target_pct_of_premium: float  # e.g. 0.30 = 30% premium rise triggers target

    def __post_init__(self) -> None:
        if self.option_type not in ("CE", "PE"):
            raise ValueError(f"recipe {self.id}: option_type must be CE or PE, got {self.option_type!r}")
        if self.max_hold_bars <= 0:
            raise ValueError(f"recipe {self.id}: max_hold_bars must be > 0")
        if self.stop_pct_of_premium <= 0 or self.stop_pct_of_premium >= 1:
            raise ValueError(f"recipe {self.id}: stop_pct must be in (0,1)")
        if self.target_pct_of_premium <= 0:
            raise ValueError(f"recipe {self.id}: target_pct must be > 0")


@dataclass(frozen=True)
class LabelContract:
    """Loaded from option_label_contract.json. Single source of truth for both
    labeler and the runtime equivalence audit."""

    lot_size: int = field(default_factory=resolve_lot_size)
    soft_close_minute: int = SOFT_CLOSE_MINUTE
    hard_close_minute: int = HARD_CLOSE_MINUTE
    min_entry_premium: float = 5.0
    min_oi_at_entry: int = 1000
    cost_model: TradingCostModel = field(default_factory=TradingCostModel)


# --- Result schema -----------------------------------------------------------

# Sentinel returned when a label is NOT emitted. The driver should drop these
# rows from the labels parquet but keep their reason in a separate skipped/
# debug parquet so we can audit drop rates.
SKIP_LABEL = -1


def make_skipped(reason: str) -> dict[str, Any]:
    """Build a label row that represents 'skipped, no label emitted'."""
    return {
        "label": SKIP_LABEL,
        "reason_skipped": reason,
        "selected_strike": None,
        "selected_expiry": None,
        "entry_premium": None,
        "exit_premium": None,
        "exit_bar_offset": None,
        "exit_reason": None,
        "gross_pnl_pct": None,
        "net_pnl_pct": None,
        "cost_pct": None,
    }


# --- Helpers -----------------------------------------------------------------


def _minute_of_day(timestamp_minute: int) -> int:
    """timestamp_minute is encoded as HH*60+MM; this is just a passthrough but
    named for readability at call sites."""
    return int(timestamp_minute)


def _compute_strike(
    *,
    atm_strike: Optional[int],
    strike_step: Optional[int],
    option_type: str,
    offset_steps: int,
) -> Optional[int]:
    """Return the chosen strike, or None if we can't compute it.

    Convention: positive offset_steps moves AWAY from the money in the
    profitable direction for the recipe's option_type.
        CE: offset +1 → higher strike (OTM call)
        PE: offset +1 → lower strike (OTM put)
    """
    if atm_strike is None or atm_strike <= 0:
        return None
    if offset_steps == 0:
        return int(atm_strike)
    if strike_step is None or strike_step <= 0:
        return None
    sign = +1 if option_type == "CE" else -1
    return int(atm_strike) + sign * int(offset_steps) * int(strike_step)


def _net_pnl_pct(
    *,
    entry_premium: float,
    exit_premium: float,
    cost_model: TradingCostModel,
    lot_size: int,
) -> tuple[float, float, float]:
    """Returns (gross_pnl_pct, cost_pct, net_pnl_pct) — all as fraction of
    entry value (entry_premium * lot_size). Lot size matters because the
    brokerage portion of TradingCostModel is a flat Rs.40 round-trip, so
    cost-as-%-of-entry scales inversely with lot size."""
    entry_value = float(entry_premium) * float(lot_size)
    exit_value = float(exit_premium) * float(lot_size)
    gross_pnl_inr = exit_value - entry_value
    breakdown = cost_model.breakdown(entry_value=entry_value, exit_value=exit_value)
    cost_inr = float(breakdown["total_cost_amount"])
    net_pnl_inr = gross_pnl_inr - cost_inr
    gross_pnl_pct = gross_pnl_inr / entry_value if entry_value > 0 else 0.0
    cost_pct = cost_inr / entry_value if entry_value > 0 else 0.0
    net_pnl_pct = net_pnl_inr / entry_value if entry_value > 0 else 0.0
    return gross_pnl_pct, cost_pct, net_pnl_pct


# --- Premium lookup interface ------------------------------------------------


class PremiumLookup:
    """Abstract premium lookup. Implementations (parquet-backed or in-memory
    for tests) must return the close price for a contract at a given minute,
    or None if missing.

    The labeler talks only to this interface so the same code is exercised by
    tests (with synthetic data) and by the real driver (with parquet)."""

    def get_close(
        self,
        *,
        timestamp_minute: int,
        trade_date: str,
        strike: int,
        option_type: str,
        expiry_str: Optional[str],
    ) -> Optional[float]:
        raise NotImplementedError

    def get_oi(
        self,
        *,
        timestamp_minute: int,
        trade_date: str,
        strike: int,
        option_type: str,
        expiry_str: Optional[str],
    ) -> Optional[float]:
        raise NotImplementedError


# --- Core labeling -----------------------------------------------------------


def label_one(
    *,
    snapshot: dict[str, Any],
    recipe: Recipe,
    lookup: PremiumLookup,
    contract: LabelContract,
) -> dict[str, Any]:
    """Compute a label for one (snapshot, recipe) pair.

    `snapshot` is a dict with keys:
        - timestamp_minute: int (HH*60+MM IST)
        - trade_date: str (YYYY-MM-DD)
        - atm_strike: Optional[int]
        - strike_step: Optional[int]
        - expiry_str: Optional[str]  (the expiry the snapshot's chain belongs to)

    Returns a label row dict. The dict always has `label` and `reason_skipped`
    keys; on skip, label=SKIP_LABEL and reason_skipped is set. On success,
    label in {0,1} and reason_skipped="".
    """
    t0_min = _minute_of_day(snapshot["timestamp_minute"])

    # Gate 1: don't open new positions after SOFT_CLOSE — runtime won't either.
    if t0_min >= contract.soft_close_minute:
        return make_skipped("entry_after_soft_close")

    # Gate 2: ensure max_hold fits before HARD_CLOSE — otherwise label can't
    # describe a real-runtime outcome.
    if t0_min + recipe.max_hold_bars > contract.hard_close_minute:
        return make_skipped("max_hold_exceeds_hard_close")

    # Gate 3: ATM strike must be present.
    strike = _compute_strike(
        atm_strike=snapshot.get("atm_strike"),
        strike_step=snapshot.get("strike_step"),
        option_type=recipe.option_type,
        offset_steps=recipe.strike_offset_steps,
    )
    if strike is None:
        return make_skipped("missing_atm_or_strike_step")

    trade_date = str(snapshot["trade_date"])
    expiry_str = snapshot.get("expiry_str")

    # Entry premium
    entry_premium = lookup.get_close(
        timestamp_minute=t0_min,
        trade_date=trade_date,
        strike=strike,
        option_type=recipe.option_type,
        expiry_str=expiry_str,
    )
    if entry_premium is None:
        return make_skipped("missing_strike_at_entry")
    if entry_premium <= 0:
        return make_skipped("zero_or_negative_entry_premium")
    if entry_premium < contract.min_entry_premium:
        return make_skipped("premium_below_min_entry_premium")

    # Liquidity gate (OI at entry)
    entry_oi = lookup.get_oi(
        timestamp_minute=t0_min,
        trade_date=trade_date,
        strike=strike,
        option_type=recipe.option_type,
        expiry_str=expiry_str,
    )
    if entry_oi is not None and entry_oi < contract.min_oi_at_entry:
        return make_skipped("oi_below_min")

    stop_price = entry_premium * (1.0 - recipe.stop_pct_of_premium)
    target_price = entry_premium * (1.0 + recipe.target_pct_of_premium)

    # Walk forward bar-by-bar, close-only check (matches runtime tracker).
    exit_premium: Optional[float] = None
    exit_offset: Optional[int] = None
    exit_reason: Optional[str] = None

    for offset in range(1, recipe.max_hold_bars + 1):
        t_min = t0_min + offset
        # If we crossed HARD_CLOSE during the walk, force-exit at hard close
        # using whatever premium we have at the hard-close minute. Runtime
        # would force-exit at the same minute.
        if t_min > contract.hard_close_minute:
            # Already enforced by gate 2 — this is defensive.
            return make_skipped("walked_past_hard_close_unexpected")

        premium_t = lookup.get_close(
            timestamp_minute=t_min,
            trade_date=trade_date,
            strike=strike,
            option_type=recipe.option_type,
            expiry_str=expiry_str,
        )
        if premium_t is None:
            # The runtime would have no quote and couldn't act on this minute.
            # Skipping the label is the safe call — we can't fabricate what would
            # have happened with no data.
            return make_skipped(f"missing_strike_at_t_plus_{offset}")
        if premium_t <= 0:
            return make_skipped(f"zero_or_negative_premium_at_t_plus_{offset}")

        # Stop check FIRST (conservative — if both stop+target hit on same close,
        # we record the loss; this also mirrors the runtime tracker's exit order).
        if premium_t <= stop_price:
            exit_premium = premium_t
            exit_offset = offset
            exit_reason = "STOP"
            break
        if premium_t >= target_price:
            exit_premium = premium_t
            exit_offset = offset
            exit_reason = "TARGET"
            break

    # If neither stop nor target hit, max hold expired — exit at the last bar's close.
    if exit_premium is None:
        # The last loop iteration's premium is what we'd use, but the loop above
        # only stored it on hit. Re-fetch at t0+max_hold.
        exit_offset = recipe.max_hold_bars
        t_exit_min = t0_min + exit_offset
        exit_premium = lookup.get_close(
            timestamp_minute=t_exit_min,
            trade_date=trade_date,
            strike=strike,
            option_type=recipe.option_type,
            expiry_str=expiry_str,
        )
        if exit_premium is None or exit_premium <= 0:
            return make_skipped("missing_strike_at_max_hold")
        exit_reason = "MAX_HOLD"

    gross_pct, cost_pct, net_pct = _net_pnl_pct(
        entry_premium=entry_premium,
        exit_premium=exit_premium,
        cost_model=contract.cost_model,
        lot_size=contract.lot_size,
    )

    label_val = 1 if net_pct > 0.0 else 0

    return {
        "label": label_val,
        "reason_skipped": "",
        "selected_strike": int(strike),
        "selected_expiry": expiry_str,
        "entry_premium": float(entry_premium),
        "exit_premium": float(exit_premium),
        "exit_bar_offset": int(exit_offset),
        "exit_reason": str(exit_reason),
        "gross_pnl_pct": float(gross_pct),
        "net_pnl_pct": float(net_pct),
        "cost_pct": float(cost_pct),
    }


__all__ = [
    "Recipe",
    "LabelContract",
    "PremiumLookup",
    "label_one",
    "make_skipped",
    "SKIP_LABEL",
]

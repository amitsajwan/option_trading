"""Historical exit-policy replay — tests scalper vs lottery on real BANKNIFTY price action.

WHY THIS EXISTS
---------------
The ML entry model (E6) cannot be trusted on 2020–2024 historical data because it was
trained on recent feature distributions. But the EXIT POLICIES are pure price-action
math (pnl_pct, mfe_pct, bars_held) — they work correctly on any era's option LTP series.

This module answers the one question historical data CAN answer:
  "On a real BANKNIFTY tail-move day, how much does each exit stack capture?"

METHODOLOGY
-----------
For each historical trading day:
  1. Load the ATM (or OTM-N) option LTP series from parquet.
  2. Enter synthetically at a fixed bar (default bar 5 = ~09:20 IST, after ORB settles).
  3. Simulate BOTH CE and PE entries independently (direction-agnostic).
  4. Tick forward bar-by-bar, updating PositionContext state.
  5. Run each exit stack at every bar; record when it fires.
  6. Output: per-trade P&L, MFE, exit reason, bars held.

WHAT THE OUTPUT TELLS YOU
--------------------------
- On flat/choppy days: which stack limits losses better?
- On fat-tail days (underlying moved >3%): which stack captures more of the move?
- The lottery thesis: rare big days must pay for many small-loss days.
- Compare expectancy and profit-factor across 4 years of real price action.

NOTE: synthetic entries are NOT the same as live entries. This study isolates EXIT
quality only. Entry quality (how often E6 finds the right direction) is a separate
question that requires accumulating live data.

Public API
----------
run_exit_replay(date_from, date_to, stacks, ...) -> ExitReplayResult
    Run both stacks on every available day in the range.

ExitReplayResult.to_report() -> str
    Markdown report with per-day table and aggregate comparison.

CLI
---
    python -m strategy_app.sim.exit_replay \
        --from 2023-01-01 --to 2024-12-31 \
        --entry-bar 5 --otm-steps 0 \
        --out docs/reports/
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_PARQUET_BASE = os.getenv("PARQUET_BASE", "/app/.data/ml_pipeline/parquet_data")
_BANKNIFTY_STEP = 100    # strike step in points


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class TradeOutcome:
    trade_date:   str
    direction:    str          # CE or PE
    entry_bar:    int
    entry_ltp:    float
    exit_bar:     int
    exit_ltp:     float
    pnl_pct:      float        # (exit_ltp - entry_ltp) / entry_ltp
    mfe_pct:      float        # max(pnl_pct series)
    mae_pct:      float        # min(pnl_pct series)
    capture_ratio: float       # pnl / mfe; NaN when mfe == 0
    bars_held:    int
    exit_reason:  str
    stack_name:   str
    strike:       int
    otm_steps:    int
    underlying_move_pct: float  # futures close/open - 1 for the day


@dataclass
class DayOutcomes:
    trade_date: str
    outcomes:   List[TradeOutcome] = field(default_factory=list)
    error:      Optional[str] = None
    underlying_move_pct: float = 0.0


@dataclass
class StackAggregate:
    stack_name:    str
    trades:        int = 0
    wins:          int = 0
    cumulative_pnl: float = 0.0
    expectancy:    float = 0.0
    profit_factor: float = 0.0
    max_drawdown:  float = 0.0
    fat_tail_capture: float = 0.0   # mean capture_ratio on fat-tail days
    fat_tail_trades: int = 0


@dataclass
class ExitReplayResult:
    date_from:   str
    date_to:     str
    entry_bar:   int
    otm_steps:   int
    days:        List[DayOutcomes] = field(default_factory=list)
    aggregates:  Dict[str, StackAggregate] = field(default_factory=dict)
    error_days:  int = 0

    def to_report(self) -> str:
        return _render_exit_report(self)


# ── Minimal position simulator ────────────────────────────────────────────────

def _simulate_exit_stacks(
    ltp_series: List[float],
    entry_bar: int,
    direction: str,
    strike: int,
    otm_steps: int,
    stacks: Dict[str, object],   # {stack_name: CompositeExitPolicy}
    underlying_move_pct: float,
    trade_date: str,
) -> List[TradeOutcome]:
    """Simulate each stack on a single direction entry. Returns one outcome per stack."""
    if entry_bar >= len(ltp_series):
        return []

    entry_ltp = ltp_series[entry_bar]
    if not entry_ltp or entry_ltp <= 0:
        return []

    from strategy_app.contracts import ExitReason, PositionContext

    outcomes: List[TradeOutcome] = []

    for stack_name, stack in stacks.items():
        pos = PositionContext(
            position_id=str(uuid.uuid4())[:8],
            direction=direction,
            strike=strike,
            expiry=None,
            entry_premium=entry_ltp,
            entry_time=datetime.now(timezone.utc),
            entry_snapshot_id="",
            lots=1,
            current_shadow_score=0.0,
        )

        mfe = 0.0
        mae = 0.0
        exit_bar = len(ltp_series) - 1
        exit_ltp = ltp_series[-1]
        exit_reason = "timestop_eod"

        for bar_idx in range(entry_bar + 1, len(ltp_series)):
            current_ltp = ltp_series[bar_idx]
            if not current_ltp or current_ltp <= 0:
                continue

            pnl = (current_ltp - entry_ltp) / entry_ltp
            mfe = max(mfe, pnl)
            mae = min(mae, pnl)
            bars = bar_idx - entry_bar

            pos.current_premium = current_ltp
            pos.pnl_pct = pnl
            pos.mfe_pct = mfe
            pos.mae_pct = mae
            pos.bars_held = bars

            reason = stack.check(pos, None)   # snap=None; policies don't use snap fields
            if reason is not None:
                exit_bar = bar_idx
                exit_ltp = current_ltp
                exit_reason = reason.value if hasattr(reason, "value") else str(reason)
                break

        final_pnl = (exit_ltp - entry_ltp) / entry_ltp
        cap = final_pnl / mfe if mfe > 0 else float("nan")

        outcomes.append(TradeOutcome(
            trade_date=trade_date,
            direction=direction,
            entry_bar=entry_bar,
            entry_ltp=entry_ltp,
            exit_bar=exit_bar,
            exit_ltp=exit_ltp,
            pnl_pct=final_pnl,
            mfe_pct=mfe,
            mae_pct=mae,
            capture_ratio=cap,
            bars_held=exit_bar - entry_bar,
            exit_reason=exit_reason,
            stack_name=stack_name,
            strike=strike,
            otm_steps=otm_steps,
            underlying_move_pct=underlying_move_pct,
        ))

    return outcomes


# ── Historical data loading ───────────────────────────────────────────────────

def _load_ltp_series(
    parquet_base: str,
    trade_date: str,
    direction: str,
    otm_steps: int,
) -> Tuple[List[float], int, float]:
    """Load intraday LTP series for (ATM + otm_steps) strike.

    Returns (ltp_series, strike, underlying_move_pct).
    Empty ltp_series means no data for this day.
    """
    from snapshot_app.historical.parquet_store import ParquetStore

    store = ParquetStore(parquet_base)
    try:
        futures_df = store.futures_window(trade_date, lookback_days=0)
        options_df = store.options_for_day(trade_date)
    finally:
        store.close()

    if futures_df is None or len(futures_df) == 0:
        return [], 0, 0.0
    if options_df is None or len(options_df) == 0:
        return [], 0, 0.0

    # Underlying move: futures close / open - 1 on this day
    day_fut = futures_df[futures_df["trade_date"].astype(str) == trade_date]
    if len(day_fut) == 0:
        day_fut = futures_df
    underlying_open  = float(day_fut["open"].iloc[0])   if len(day_fut) else 0.0
    underlying_close = float(day_fut["close"].iloc[-1]) if len(day_fut) else 0.0
    underlying_move = (underlying_close - underlying_open) / underlying_open if underlying_open else 0.0

    # ATM: round futures open to nearest strike step
    atm = round(underlying_open / _BANKNIFTY_STEP) * _BANKNIFTY_STEP
    if otm_steps > 0:
        strike = atm + otm_steps * _BANKNIFTY_STEP if direction == "CE" else atm - otm_steps * _BANKNIFTY_STEP
    else:
        strike = atm

    # Filter options for this strike + direction
    opt_type = direction  # "CE" or "PE"
    mask = (
        (options_df["option_type"].astype(str).str.upper() == opt_type) &
        (options_df["strike"].round().astype(int) == int(strike))
    )
    strike_df = options_df[mask].copy()
    if len(strike_df) == 0:
        # Try nearest available strike
        available = options_df[options_df["option_type"].astype(str).str.upper() == opt_type]["strike"].dropna()
        if len(available) == 0:
            return [], strike, underlying_move
        nearest = int(available.map(lambda s: abs(float(s) - strike)).idxmin())
        strike = int(options_df.loc[nearest, "strike"])
        mask = (
            (options_df["option_type"].astype(str).str.upper() == opt_type) &
            (options_df["strike"].round().astype(int) == int(strike))
        )
        strike_df = options_df[mask].copy()

    strike_df = strike_df.sort_values("timestamp")
    # Use close price as the bar LTP
    ltp_series = [float(v) for v in strike_df["close"].tolist() if v is not None and not _isnan(v)]
    return ltp_series, strike, underlying_move


def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except Exception:
        return False


# ── Main runner ───────────────────────────────────────────────────────────────

def run_exit_replay(
    date_from: str,
    date_to: str,
    stacks: Optional[Dict[str, object]] = None,
    parquet_base: Optional[str] = None,
    entry_bar: int = 5,
    otm_steps: int = 0,
    fat_tail_threshold: float = 0.03,
    directions: Optional[List[str]] = None,
) -> ExitReplayResult:
    """Run both (or given) exit stacks over every available historical day.

    Args:
        date_from:          "YYYY-MM-DD" start (inclusive).
        date_to:            "YYYY-MM-DD" end (inclusive).
        stacks:             Dict of {name: CompositeExitPolicy}. Defaults to
                            scalper and lottery built from current env vars.
        parquet_base:       Path to parquet data root.
        entry_bar:          Bar index to enter (0 = first bar of session). Default 5 (~09:20).
        otm_steps:          0 = ATM, 1 = 1 step OTM, etc.
        fat_tail_threshold: Underlying move % to classify a "fat-tail day". Default 3%.
        directions:         ["CE", "PE"] by default — both entered independently.

    Returns:
        ExitReplayResult with per-day outcomes and stack aggregates.
    """
    if stacks is None:
        from strategy_app.position.exit_policy import build_scalper_exit_stack, build_lottery_exit_stack
        stacks = {
            "scalper": build_scalper_exit_stack(),
            "lottery": build_lottery_exit_stack(),
        }

    base = parquet_base or _DEFAULT_PARQUET_BASE
    directions = directions or ["CE", "PE"]

    from strategy_app.sim.snapshot_loader import available_days
    days = available_days(base, date_from=date_from, date_to=date_to)

    result = ExitReplayResult(
        date_from=date_from,
        date_to=date_to,
        entry_bar=entry_bar,
        otm_steps=otm_steps,
    )

    for trade_date in days:
        day_out = DayOutcomes(trade_date=trade_date)

        all_outcomes: List[TradeOutcome] = []
        for direction in directions:
            try:
                ltp_series, strike, underlying_move = _load_ltp_series(
                    base, trade_date, direction, otm_steps,
                )
                day_out.underlying_move_pct = underlying_move

                if not ltp_series:
                    continue

                outcomes = _simulate_exit_stacks(
                    ltp_series=ltp_series,
                    entry_bar=entry_bar,
                    direction=direction,
                    strike=strike,
                    otm_steps=otm_steps,
                    stacks=stacks,
                    underlying_move_pct=underlying_move,
                    trade_date=trade_date,
                )
                all_outcomes.extend(outcomes)

            except Exception as exc:
                logger.exception("exit_replay: %s %s failed: %s", trade_date, direction, exc)
                day_out.error = str(exc)
                result.error_days += 1

        day_out.outcomes = all_outcomes
        result.days.append(day_out)

    _aggregate_exit_results(result, stacks, fat_tail_threshold)
    return result


def _aggregate_exit_results(
    result: ExitReplayResult,
    stacks: Dict[str, object],
    fat_tail_threshold: float,
) -> None:
    for name in stacks:
        agg = StackAggregate(stack_name=name)
        pnls: List[float] = []
        wins: List[float] = []
        losses: List[float] = []
        fat_caps: List[float] = []

        for day in result.days:
            for o in day.outcomes:
                if o.stack_name != name:
                    continue
                agg.trades += 1
                pnls.append(o.pnl_pct)
                if o.pnl_pct > 0:
                    wins.append(o.pnl_pct)
                    agg.wins += 1
                elif o.pnl_pct < 0:
                    losses.append(o.pnl_pct)
                if abs(day.underlying_move_pct) >= fat_tail_threshold:
                    if not math.isnan(o.capture_ratio):
                        fat_caps.append(o.capture_ratio)
                        agg.fat_tail_trades += 1

        agg.cumulative_pnl = sum(pnls)
        agg.expectancy = sum(pnls) / len(pnls) if pnls else 0.0
        agg.profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
        agg.fat_tail_capture = sum(fat_caps) / len(fat_caps) if fat_caps else float("nan")

        # Max drawdown on cumulative P&L curve (per trade, not per day)
        cum = 0.0; peak = 0.0; max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        agg.max_drawdown = max_dd

        result.aggregates[name] = agg


# ── Markdown report ──────────────────────────────────────────────────────────

def _render_exit_report(r: ExitReplayResult) -> str:
    lines: List[str] = []

    def _h(n, t):
        lines.append(f"{'#'*n} {t}"); lines.append("")

    def _row(*cols):
        return "| " + " | ".join(str(c) for c in cols) + " |"

    _h(1, "Exit Policy Replay Report")
    lines.append(f"Period: **{r.date_from}** → **{r.date_to}**  |  ")
    lines.append(f"Entry bar: {r.entry_bar}  |  OTM steps: {r.otm_steps}  |  ")
    lines.append(f"Error days: {r.error_days}")
    lines.append("")
    lines.append("> **Note:** Entries are synthetic (fixed bar, ATM/OTM strike).")
    lines.append("> This study isolates EXIT quality only — not entry quality.")
    lines.append("")

    _h(2, "Stack Comparison")
    lines.append(_row("Metric", *r.aggregates.keys()))
    lines.append(_row("---", *["---"] * len(r.aggregates)))
    aggs = list(r.aggregates.values())

    def _pf(v):
        return f"{v:.2f}" if math.isfinite(v) else "∞"

    for label, getter in [
        ("Trades", lambda a: a.trades),
        ("Win rate", lambda a: f"{100*a.wins/max(a.trades,1):.0f}%"),
        ("Cumulative P&L", lambda a: f"{a.cumulative_pnl:+.2f}%"),
        ("Expectancy (per trade)", lambda a: f"{a.expectancy:+.4f}%"),
        ("Profit factor", lambda a: _pf(a.profit_factor)),
        ("Max drawdown", lambda a: f"{a.max_drawdown:.2f}%"),
        (f"Fat-tail capture (avg)", lambda a: f"{a.fat_tail_capture:.2f}" if not math.isnan(a.fat_tail_capture) else "—"),
        ("Fat-tail trades", lambda a: a.fat_tail_trades),
    ]:
        lines.append(_row(label, *[getter(a) for a in aggs]))
    lines.append("")

    _h(2, "Per-Day Summary")
    stack_names = list(r.aggregates.keys())
    header = ["Date", "Underlying%"] + [f"{n} CE pnl" for n in stack_names] + [f"{n} PE pnl" for n in stack_names]
    lines.append(_row(*header))
    lines.append(_row(*["---"] * len(header)))

    for day in r.days:
        row: List[str] = [day.trade_date, f"{day.underlying_move_pct:+.1%}"]
        for direction in ["CE", "PE"]:
            for sname in stack_names:
                match = [o for o in day.outcomes if o.direction == direction and o.stack_name == sname]
                cell = f"{match[0].pnl_pct:+.2%} ({match[0].exit_reason[:8]})" if match else "—"
                row.append(cell)
        lines.append(_row(*row))
    lines.append("")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Historical exit-policy replay")
    parser.add_argument("--from",   dest="date_from", required=True)
    parser.add_argument("--to",     dest="date_to",   required=True)
    parser.add_argument("--entry-bar", type=int, default=5)
    parser.add_argument("--otm-steps", type=int, default=0)
    parser.add_argument("--fat-tail", type=float, default=0.03,
                        help="Underlying move threshold for fat-tail day (default 3%%)")
    parser.add_argument("--parquet-base", default=None)
    parser.add_argument("--out", default="docs/reports/")
    args = parser.parse_args()

    result = run_exit_replay(
        date_from=args.date_from,
        date_to=args.date_to,
        entry_bar=args.entry_bar,
        otm_steps=args.otm_steps,
        fat_tail_threshold=args.fat_tail,
        parquet_base=args.parquet_base,
    )

    report = result.to_report()
    print(report)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fname = f"exit_replay_{args.date_from}_{args.date_to}_otm{args.otm_steps}.md"
    (out / fname).write_text(report, encoding="utf-8")
    print(f"\nReport → {out / fname}")


if __name__ == "__main__":
    _cli()

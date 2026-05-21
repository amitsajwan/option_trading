from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from .condition_evaluator import evaluate_condition
from .rule_schema import Condition, ExitConfig, Rule


@dataclass
class Trade:
    trade_date: str
    entry_minute: int
    exit_minute: int
    entry_premium: float
    exit_premium: float
    net_pnl_pct: float
    exit_reason: str
    mfe_pct: float
    mae_pct: float


def _premium_col(direction: str) -> str:
    return "ce_close" if "CE" in direction.upper() else "pe_close"


def _is_short(direction: str) -> bool:
    """Direction names like 'SELL_ATM_CE' or 'SELL_ATM_PE' open a short
    position. 'BUY_*' (or anything else) is treated as long."""
    return direction.upper().startswith("SELL_")


def _position_pnl(entry_premium: float, current_premium: float, direction: str) -> float:
    """Return P&L as a fraction of entry premium, sign-correct for the
    position side. Positive = favorable for the trader; negative = adverse.

    Long: profit when premium rises.
    Short: profit when premium drops.

    A stop_pct of 100 with a short means "premium has doubled" (loss of
    one full credit). target_pct of 50 with a short means "premium has
    halved" (classic 50%-of-credit short-option target).
    """
    if entry_premium <= 0:
        return 0.0
    if _is_short(direction):
        return (entry_premium - current_premium) / entry_premium
    return (current_premium - entry_premium) / entry_premium


def _get_premium(row, direction: str) -> Optional[float]:
    col = _premium_col(direction)
    val = getattr(row, col, None)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


def _underlying_adverse(
    df: pd.DataFrame,
    idx: int,
    entry_idx: int,
    direction: str,
    underlying_stop_pct: float,
) -> bool:
    if underlying_stop_pct <= 0 or "px_fut_close" not in df.columns:
        return False
    entry_fut = pd.to_numeric(df.iloc[entry_idx]["px_fut_close"], errors="coerce")
    curr_fut = pd.to_numeric(df.iloc[idx]["px_fut_close"], errors="coerce")
    if pd.isna(entry_fut) or pd.isna(curr_fut) or entry_fut <= 0:
        return False
    move = (float(curr_fut) - float(entry_fut)) / float(entry_fut)
    if _is_short(direction):
        return move > underlying_stop_pct
    return move < -underlying_stop_pct


def _evaluate_exit_conditions(
    df: pd.DataFrame,
    idx: int,
    exit_cfg: ExitConfig,
    entry_premium: float,
    direction: str,
    *,
    entry_idx: int,
    mfe: float,
) -> Optional[str]:
    row = df.iloc[idx]
    premium = _get_premium(row, direction)
    if premium is None or entry_premium <= 0:
        return None

    pnl = _position_pnl(entry_premium, premium, direction)

    if exit_cfg.underlying_stop_pct is not None:
        if _underlying_adverse(df, idx, entry_idx, direction, exit_cfg.underlying_stop_pct):
            return "underlying_stop"

    if pnl <= -exit_cfg.stop_pct / 100:
        return "stop_loss"
    if pnl >= exit_cfg.target_pct / 100:
        return "target"

    if (
        exit_cfg.trail_activation_pct is not None
        and exit_cfg.trail_giveback_pct is not None
        and mfe >= exit_cfg.trail_activation_pct / 100
        and pnl <= mfe - exit_cfg.trail_giveback_pct / 100
    ):
        return "trail_stop"

    if exit_cfg.signal_exits:
        # Slice preserves all columns + index, so cross-column conditions
        # (e.g., "px_fut_close < vwap_fut") resolve correctly.
        row_df = df.iloc[idx:idx + 1]
        for cond in exit_cfg.signal_exits:
            if evaluate_condition(row_df, cond).iloc[0]:
                return f"signal:{cond.column}"

    return None


def simulate_trades(
    df: pd.DataFrame,
    rule: Rule,
    exit_mode: str,
    *,
    cost_bps: float = 2.0,
) -> pd.DataFrame:
    """Walk df, fire on rule.signal, hold under exit_cfg, return one row per trade.

    Output columns: trade_date, entry_minute, exit_minute, entry_premium,
    exit_premium, net_pnl_pct, exit_reason, mfe_pct, mae_pct.

    To audit the resulting trades with `audit_run.audit`, pass
    return_col="net_pnl_pct" and date_col="trade_date".
    """
    if "signal" not in df.columns:
        raise ValueError("df must have 'signal' column — run generate_signals first")

    exit_cfg = rule.exit_mechanical if exit_mode == "mechanical" else rule.exit_signal
    if exit_cfg is None:
        raise ValueError(f"rule {rule.rule_id} has no exit_signal config")

    df = df.sort_values(["trade_date", "minute"]).reset_index(drop=True)
    direction = rule.direction

    trades: list[dict] = []
    last_date: Optional[str] = None
    blocked_until_min: int = -1

    for i in range(len(df)):
        row = df.iloc[i]
        td = str(row["trade_date"])[:10]

        if td != last_date:
            blocked_until_min = -1
            last_date = td

        minute = int(row["minute"])
        if minute < blocked_until_min:
            continue

        if not bool(row["signal"]):
            continue

        entry_premium = _get_premium(row, direction)
        if entry_premium is None or entry_premium <= 0:
            continue

        exit_minute, exit_premium, exit_reason, mfe, mae = _walk_exit(
            df, i, td, minute, entry_premium, exit_cfg, direction,
        )

        if exit_premium is None or exit_premium <= 0:
            continue

        # Returns stored as decimal fractions (e.g. 0.05 = +5%) to match the
        # convention in audit_run.audit and the engine's POSITION_CLOSE events.
        # _position_pnl handles long/short sign convention. Cost = 2 bps = 0.0002.
        gross_pnl = _position_pnl(entry_premium, exit_premium, direction)
        net_pnl = gross_pnl - cost_bps / 10000

        trades.append({
            "trade_date": td,
            "entry_minute": minute,
            "exit_minute": exit_minute,
            "entry_premium": round(entry_premium, 4),
            "exit_premium": round(exit_premium, 4),
            "net_pnl_pct": round(net_pnl, 6),
            "exit_reason": exit_reason,
            "mfe_pct": round(mfe, 6),
            "mae_pct": round(mae, 6),
        })

        blocked_until_min = exit_minute

    return pd.DataFrame(trades)


def _walk_exit(
    df: pd.DataFrame,
    entry_idx: int,
    trade_date: str,
    entry_minute: int,
    entry_premium: float,
    exit_cfg: ExitConfig,
    direction: str,
) -> tuple[int, Optional[float], str, float, float]:
    mfe = 0.0
    mae = 0.0
    last_same_day: Optional[tuple[int, Optional[float]]] = None

    for j in range(entry_idx + 1, len(df)):
        row = df.iloc[j]
        td = str(row["trade_date"])[:10]
        if td != trade_date:
            break

        minute = int(row["minute"])
        premium = _get_premium(row, direction)
        last_same_day = (minute, premium)

        if premium is not None and entry_premium > 0:
            pnl = _position_pnl(entry_premium, premium, direction)
            mfe = max(mfe, pnl)
            mae = min(mae, pnl)

        reason = _evaluate_exit_conditions(
            df, j, exit_cfg, entry_premium, direction, entry_idx=entry_idx, mfe=mfe,
        )
        if reason:
            return minute, premium, reason, mfe, mae

        if minute - entry_minute >= exit_cfg.time_stop_minutes:
            return minute, premium, "time_stop", mfe, mae

        if minute >= exit_cfg.eod_force_close_minute:
            return minute, premium, "eod_force", mfe, mae

    # Fell off the end of the day (or end of df) without a mechanical exit.
    # last_same_day is None only if entry was the final row in df.
    if last_same_day is None:
        return entry_minute, entry_premium, "eod_force", mfe, mae
    minute, premium = last_same_day
    return minute, premium, "eod_force", mfe, mae

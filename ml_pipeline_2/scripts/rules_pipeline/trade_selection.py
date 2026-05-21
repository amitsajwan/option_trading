"""Per-day trade caps and ranking for rules backtests."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .rule_schema import Rule, TradeScoreConfig


def _row_score(df: pd.DataFrame, idx: int, cfg: TradeScoreConfig) -> float:
    if cfg.mode == "first":
        return -float(df.at[idx, "minute"])
    total = 0.0
    weights = cfg.weights if cfg.weights else tuple(1.0 for _ in cfg.columns)
    for col, w in zip(cfg.columns, weights):
        if col not in df.columns:
            continue
        val = pd.to_numeric(df.at[idx, col], errors="coerce")
        if pd.isna(val):
            continue
        if cfg.use_abs:
            val = abs(float(val))
        total += float(w) * val
    return total


def apply_daily_trade_cap(
    df: pd.DataFrame,
    signals: pd.Series,
    rule: Rule,
) -> pd.Series:
    """Keep at most ``rule.max_trades_per_day`` entry signals per trade_date.

    Ranking:
      - trade_score.mode == \"first\" → earliest minutes (lowest time_minute_of_day)
      - else → highest composite score from trade_score columns/weights
    """
    cap = rule.max_trades_per_day
    if cap is None or cap <= 0:
        return signals

    out = signals.astype(bool).copy()
    score_cfg = rule.trade_score or TradeScoreConfig(mode="first")

    if "trade_date" not in df.columns:
        raise ValueError("df must contain trade_date for daily trade cap")

    dates = pd.to_datetime(df["trade_date"]).dt.normalize()
    for td in dates.unique():
        day_mask = dates == td
        fired = day_mask & out
        n = int(fired.sum())
        if n <= cap:
            continue
        idx = df.index[fired].tolist()
        if score_cfg.mode == "first":
            idx.sort(key=lambda i: (float(df.at[i, "minute"]), i))
            keep = idx[:cap]
        else:
            scored = [(i, _row_score(df, i, score_cfg)) for i in idx]
            scored.sort(key=lambda x: (-x[1], float(df.at[x[0], "minute"]), x[0]))
            keep = [i for i, _ in scored[:cap]]
        drop = set(idx) - set(keep)
        for i in drop:
            out.at[i] = False
    return out

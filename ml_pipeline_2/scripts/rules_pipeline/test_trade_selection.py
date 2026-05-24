from __future__ import annotations

import pandas as pd

from ml_pipeline_2.scripts.rules_pipeline.rule_schema import (
    Condition,
    ExitConfig,
    Rule,
    TradeScoreConfig,
)
from ml_pipeline_2.scripts.rules_pipeline.signal_generator import generate_signals
from ml_pipeline_2.scripts.rules_pipeline.trade_selection import apply_daily_trade_cap


def _r1s_rule(**kwargs) -> Rule:
    base = dict(
        rule_id="TEST",
        direction="SELL_ATM_CE",
        entry_conditions=(
            Condition("ctx_opening_range_ready", "==", 1),
            Condition("ctx_opening_range_breakout_down", "==", 1),
            Condition("ret_5m", "<", 0),
            Condition("vwap_distance", "<", 0),
        ),
        disqualifiers=(),
        exit_mechanical=ExitConfig(100, 50, 20, 920),
    )
    base.update(kwargs)
    return Rule(**base)


def test_top3_first_keeps_earliest_minutes():
    df = pd.DataFrame(
        {
            "trade_date": ["2024-08-15"] * 5,
            "minute": [600, 610, 620, 630, 640],
            "ctx_opening_range_ready": [1] * 5,
            "ctx_opening_range_breakout_down": [1] * 5,
            "ret_5m": [-0.001] * 5,
            "vwap_distance": [-0.002] * 5,
        }
    )
    rule = _r1s_rule(max_trades_per_day=3, trade_score=TradeScoreConfig(mode="first"))
    raw = generate_signals(df, rule)
    assert int(raw.sum()) == 5
    capped = apply_daily_trade_cap(df, raw, rule)
    assert int(capped.sum()) == 3
    kept = df.loc[capped, "minute"].tolist()
    assert kept == [600, 610, 620]


def test_top3_ret5m_prefers_largest_move():
    df = pd.DataFrame(
        {
            "trade_date": ["2024-08-15"] * 4,
            "minute": [600, 610, 620, 630],
            "ctx_opening_range_ready": [1] * 4,
            "ctx_opening_range_breakout_down": [1] * 4,
            "ret_5m": [-0.001, -0.005, -0.002, -0.010],
            "vwap_distance": [-0.001] * 4,
        }
    )
    rule = _r1s_rule(
        max_trades_per_day=3,
        trade_score=TradeScoreConfig(mode="columns", columns=("ret_5m",), weights=(1.0,)),
    )
    raw = generate_signals(df, rule)
    capped = apply_daily_trade_cap(df, raw, rule)
    kept = set(df.loc[capped, "minute"].tolist())
    assert kept == {610, 620, 630}


def test_no_cap_passthrough():
    df = pd.DataFrame(
        {
            "trade_date": ["2024-08-15"] * 2,
            "minute": [600, 610],
            "ctx_opening_range_ready": [1, 1],
            "ctx_opening_range_breakout_down": [1, 1],
            "ret_5m": [-0.001, -0.002],
            "vwap_distance": [-0.001, -0.001],
        }
    )
    rule = _r1s_rule(max_trades_per_day=None)
    raw = generate_signals(df, rule)
    capped = apply_daily_trade_cap(df, raw, rule)
    assert capped.equals(raw)


def test_rule_from_dict_top3():
    d = {
        "rule_id": "X",
        "direction": "SELL_ATM_CE",
        "max_trades_per_day": 3,
        "trade_score": {
            "mode": "columns",
            "columns": ["ret_5m", "vwap_distance"],
            "weights": [0.5, 0.5],
            "abs": True,
        },
        "entry_conditions": [{"column": "a", "operator": ">", "value": 0}],
        "disqualifiers": [],
        "exit_mechanical": {
            "stop_pct": 100,
            "target_pct": 50,
            "time_stop_minutes": 20,
            "eod_force_close_minute": 920,
        },
    }
    rule = Rule.from_dict(d)
    assert rule.max_trades_per_day == 3
    assert rule.trade_score is not None
    assert rule.trade_score.columns == ("ret_5m", "vwap_distance")

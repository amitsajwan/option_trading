from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from strategy_app.contracts import Direction, PositionContext, SignalType, TradeSignal
from strategy_app.tools.offline_strategy_analysis import MemorySignalLogger, TradingCostModel, _summary


def test_memory_signal_logger_applies_round_trip_costs() -> None:
    logger = MemorySignalLogger(
        capital_allocated=500000.0,
        cost_model=TradingCostModel(brokerage_per_order=20.0, charges_bps_per_side=2.5, slippage_bps_per_side=7.5),
    )
    entry_ts = datetime(2026, 4, 1, 9, 30)
    exit_ts = datetime(2026, 4, 1, 9, 45)
    entry_signal = TradeSignal(
        signal_id="entry-1",
        timestamp=entry_ts,
        snapshot_id="snap-entry",
        signal_type=SignalType.ENTRY,
        direction="CE",
        strike=50000,
        entry_premium=100.0,
        reason="[TRENDING] ORB: test",
        confidence=0.8,
        votes=[],
    )
    position = PositionContext(
        position_id="pos-1",
        direction="CE",
        strike=50000,
        expiry=date(2026, 4, 2),
        entry_premium=100.0,
        entry_time=entry_ts,
        entry_snapshot_id="snap-entry",
        lots=1,
        entry_strategy="ORB",
    )
    logger.log_position_open(entry_signal, position)

    exit_signal = TradeSignal(
        signal_id="exit-1",
        timestamp=exit_ts,
        snapshot_id="snap-exit",
        signal_type=SignalType.EXIT,
        direction="EXIT",
        position_id="pos-1",
        reason="close",
    )
    logger.log_position_close(
        exit_signal=exit_signal,
        position=position,
        entry_premium=100.0,
        exit_premium=120.0,
        pnl_pct=0.20,
        mfe_pct=0.25,
        mae_pct=-0.03,
        bars_held=5,
        stop_loss_pct=0.20,
        stop_price=80.0,
        high_water_premium=125.0,
        target_pct=0.80,
        trailing_enabled=True,
        trailing_activation_pct=0.10,
        trailing_offset_pct=0.05,
        trailing_lock_breakeven=True,
        trailing_active=True,
        orb_trail_activation_mfe=0.15,
        orb_trail_offset_pct=0.08,
        orb_trail_min_lock_pct=0.05,
        orb_trail_priority_over_regime=True,
        orb_trail_regime_filter=None,
        orb_trail_active=True,
        orb_trail_stop_price=115.0,
        oi_trail_activation_mfe=0.15,
        oi_trail_offset_pct=0.08,
        oi_trail_min_lock_pct=0.05,
        oi_trail_priority_over_regime=True,
        oi_trail_regime_filter=None,
        oi_trail_active=False,
        oi_trail_stop_price=None,
    )

    row = logger.trades[0]
    assert row["total_cost_amount"] > 0.0
    assert row["pnl_amount_net"] < row["pnl_amount_gross"]
    assert row["capital_pnl_pct"] < row["capital_pnl_pct_gross"]
    assert row["pnl_pct_net"] < row["pnl_pct_gross"]


def test_summary_uses_net_trade_returns_when_available() -> None:
    frame = pd.DataFrame(
        [
            {
                "pnl_pct": 0.20,
                "pnl_pct_net": 0.15,
                "capital_pnl_pct": 0.01,
                "mfe_pct": 0.25,
                "mae_pct": -0.03,
                "bars_held": 5,
                "lots": 1,
                "exit_reason": "TRAILING_STOP",
                "total_cost_amount": 45.0,
            },
            {
                "pnl_pct": -0.10,
                "pnl_pct_net": -0.14,
                "capital_pnl_pct": -0.005,
                "mfe_pct": 0.05,
                "mae_pct": -0.12,
                "bars_held": 4,
                "lots": 1,
                "exit_reason": "STOP_LOSS",
                "total_cost_amount": 42.0,
            },
        ]
    )

    summary = _summary(frame, capital_allocated=500000.0)
    assert summary["avg_trade_pnl_pct"] == pytest.approx(0.005)
    assert summary["avg_trade_pnl_pct_gross"] == pytest.approx(0.05)
    assert summary["total_cost_amount"] == pytest.approx(87.0)

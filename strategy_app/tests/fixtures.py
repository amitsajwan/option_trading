"""Shared test factories to reduce boilerplate across strategy_app tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from strategy_app.contracts import (
    Direction,
    PositionContext,
    RiskContext,
    SignalType,
    TradeSignal,
)
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def make_snapshot(
    *,
    timestamp: Optional[datetime] = None,
    fut_close: float = 50000.0,
    strikes: Optional[list[dict[str, Any]]] = None,
    **kwargs: Any,
) -> SnapshotAccessor:
    """Build a SnapshotAccessor with sensible defaults."""
    payload: dict[str, Any] = {
        "session_context": {
            "snapshot_id": kwargs.get("snapshot_id", "snap-001"),
            "timestamp": (timestamp or datetime(2024, 8, 1, 9, 30, tzinfo=timezone.utc)).isoformat(),
            "date": kwargs.get("trade_date", "2024-08-01"),
            "minutes_since_open": kwargs.get("minutes_since_open", 15),
        },
        "futures_bar": {"close": fut_close},
        "futures_derived": {
            "return_5m": kwargs.get("fut_return_5m", 0.0),
            "return_15m": kwargs.get("fut_return_15m", 0.0),
            "vwap": kwargs.get("vwap", fut_close),
            "price_vs_vwap": kwargs.get("price_vs_vwap", 0.0),
        },
        "strikes": strikes or [],
        "opening_range": {
            "orh": kwargs.get("orh", fut_close + 50),
            "orl": kwargs.get("orl", fut_close - 50),
            "or_ready": kwargs.get("or_ready", True),
        },
        "vix_context": {
            "vix_current": kwargs.get("vix_current", 12.0),
            "intraday_change_pct": kwargs.get("vix_intraday_chg", 0.0),
        },
    }
    if "iv_derived" in kwargs:
        payload["iv_derived"] = kwargs["iv_derived"]
    if "atm_options" in kwargs:
        payload["atm_options"] = kwargs["atm_options"]
    return SnapshotAccessor(payload)


def make_trade_signal(
    *,
    direction: Direction = Direction.CE,
    strike: int = 50000,
    entry_premium: float = 100.0,
    stop_loss_pct: float = 0.20,
    target_pct: float = 0.80,
    max_hold_bars: int = 15,
    max_lots: int = 1,
    underlying_stop_pct: Optional[float] = None,
    underlying_target_pct: Optional[float] = None,
    trailing_enabled: bool = False,
    entry_strategy_name: str = "TEST",
    **kwargs: Any,
) -> TradeSignal:
    """Build a TradeSignal with sensible defaults."""
    return TradeSignal(
        signal_id=kwargs.get("signal_id", "sig-001"),
        timestamp=kwargs.get("timestamp", datetime(2024, 8, 1, 9, 30, tzinfo=timezone.utc)),
        snapshot_id=kwargs.get("snapshot_id", "snap-001"),
        signal_type=kwargs.get("signal_type", SignalType.ENTRY),
        direction=direction,
        strike=strike,
        entry_premium=entry_premium,
        max_hold_bars=max_hold_bars,
        stop_loss_pct=stop_loss_pct,
        target_pct=target_pct,
        underlying_stop_pct=underlying_stop_pct,
        underlying_target_pct=underlying_target_pct,
        trailing_enabled=trailing_enabled,
        max_lots=max_lots,
        entry_strategy_name=entry_strategy_name,
        entry_regime_name=kwargs.get("entry_regime_name", "TEST_REGIME"),
        source=kwargs.get("source", "TEST"),
        confidence=kwargs.get("confidence", 0.80),
        reason=kwargs.get("reason", "test"),
        votes=kwargs.get("votes", []),
    )


def make_position(
    *,
    direction: str = "CE",
    strike: int = 50000,
    entry_premium: float = 100.0,
    current_premium: Optional[float] = None,
    stop_loss_pct: float = 0.20,
    target_pct: float = 0.80,
    max_hold_bars: int = 15,
    bars_held: int = 0,
    **kwargs: Any,
) -> PositionContext:
    """Build a PositionContext with sensible defaults."""
    return PositionContext(
        position_id=kwargs.get("position_id", "pos-001"),
        direction=direction,
        strike=strike,
        entry_premium=entry_premium,
        current_premium=current_premium or entry_premium,
        entry_time=kwargs.get("entry_time", datetime(2024, 8, 1, 9, 30, tzinfo=timezone.utc)),
        entry_snapshot_id=kwargs.get("entry_snapshot_id", "snap-001"),
        lots=kwargs.get("lots", 1),
        max_hold_bars=max_hold_bars,
        bars_held=bars_held,
        stop_loss_pct=stop_loss_pct,
        stop_price=kwargs.get("stop_price", entry_premium * (1.0 - stop_loss_pct)),
        target_pct=target_pct,
        pnl_pct=kwargs.get("pnl_pct", 0.0),
        mfe_pct=kwargs.get("mfe_pct", 0.0),
        mae_pct=kwargs.get("mae_pct", 0.0),
        high_water_premium=kwargs.get("high_water_premium", entry_premium),
        trailing_enabled=kwargs.get("trailing_enabled", False),
        trailing_active=kwargs.get("trailing_active", False),
        entry_strategy=kwargs.get("entry_strategy", "TEST"),
        entry_regime=kwargs.get("entry_regime", "TEST_REGIME"),
        entry_reason=kwargs.get("entry_reason", "test"),
        engine_mode=kwargs.get("engine_mode", "test"),
        decision_mode=kwargs.get("decision_mode", "test"),
        decision_reason_code=kwargs.get("decision_reason_code", "test"),
        strategy_family_version=kwargs.get("strategy_family_version", "TEST_V1"),
        strategy_profile_id=kwargs.get("strategy_profile_id", "test_profile"),
    )


def make_risk_context(**kwargs: Any) -> RiskContext:
    """Build a RiskContext with safe defaults for testing."""
    return RiskContext(
        max_daily_loss_pct=kwargs.get("max_daily_loss_pct", 0.02),
        max_session_trades=kwargs.get("max_session_trades", 6),
        max_consecutive_losses=kwargs.get("max_consecutive_losses", 3),
        max_lots_per_trade=kwargs.get("max_lots_per_trade", 5),
        capital_allocated=kwargs.get("capital_allocated", 500_000.0),
        risk_per_trade_pct=kwargs.get("risk_per_trade_pct", 0.005),
    )

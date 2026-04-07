"""Layer 3 -> Layer 4 strategy contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

SnapshotPayload = dict[str, Any]


class Direction(str, Enum):
    CE = "CE"
    PE = "PE"
    AVOID = "AVOID"
    EXIT = "EXIT"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TARGET_HIT = "TARGET_HIT"
    TIME_STOP = "TIME_STOP"
    REGIME_SHIFT = "REGIME_SHIFT"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    RISK_BREACH = "RISK_BREACH"
    MANUAL = "MANUAL"


class SignalType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    HOLD = "HOLD"
    SKIP = "SKIP"


@dataclass
class PositionContext:
    """Current open position state."""

    position_id: str
    direction: str
    strike: int
    expiry: Optional[date]
    entry_premium: float
    entry_time: datetime
    entry_snapshot_id: str
    lots: int
    signal_id: Optional[str] = None
    current_premium: float = 0.0
    pnl_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    bars_held: int = 0
    max_hold_bars: Optional[int] = None
    stop_loss_pct: float = 0.40
    stop_price: Optional[float] = None
    high_water_premium: float = 0.0
    target_pct: float = 0.80
    trailing_enabled: bool = False
    trailing_activation_pct: float = 0.10
    trailing_offset_pct: float = 0.05
    trailing_lock_breakeven: bool = True
    trailing_active: bool = False
    orb_trail_activation_mfe: float = 0.15
    orb_trail_offset_pct: float = 0.08
    orb_trail_min_lock_pct: float = 0.05
    orb_trail_priority_over_regime: bool = True
    orb_trail_regime_filter: Optional[str] = None
    orb_trail_active: bool = False
    orb_trail_stop_price: Optional[float] = None
    oi_trail_activation_mfe: float = 0.15
    oi_trail_offset_pct: float = 0.08
    oi_trail_min_lock_pct: float = 0.05
    oi_trail_priority_over_regime: bool = True
    oi_trail_regime_filter: Optional[str] = None
    oi_trail_active: bool = False
    oi_trail_stop_price: Optional[float] = None
    entry_strategy: str = ""
    entry_regime: str = ""
    entry_reason: str = ""
    decision_metrics: dict[str, Any] = field(default_factory=dict)
    engine_mode: Optional[str] = None
    decision_mode: Optional[str] = None
    decision_reason_code: Optional[str] = None
    strategy_family_version: Optional[str] = None
    strategy_profile_id: Optional[str] = None


@dataclass
class RiskContext:
    """Portfolio level risk state for the current session."""

    session_realised_pnl: float = 0.0
    session_unrealised_pnl: float = 0.0
    session_pnl_total: float = 0.0
    session_trade_count: int = 0
    consecutive_losses: int = 0
    session_loss_count: int = 0
    session_win_count: int = 0
    capital_allocated: float = 0.0
    capital_at_risk: float = 0.0
    daily_loss_breached: bool = False
    session_trade_cap_breached: bool = False
    consecutive_loss_limit: bool = False
    vix_spike_halt: bool = False
    vix_last_halt_at: Optional[datetime] = None
    vix_below_resume_since: Optional[datetime] = None
    vix_last_resume_at: Optional[datetime] = None
    post_halt_resume_boost_available: bool = False
    weekly_loss_breached: bool = False
    max_daily_loss_pct: float = 0.02
    max_session_trades: int = 6
    max_consecutive_losses: int = 3
    max_lots_per_trade: int = 5
    risk_per_trade_pct: float = 0.005


@dataclass
class StrategyVote:
    """Vote emitted by one strategy for one snapshot."""

    strategy_name: str
    snapshot_id: str
    timestamp: datetime
    trade_date: str
    signal_type: SignalType
    direction: Optional[Direction]
    confidence: float
    reason: str
    raw_signals: dict[str, Any] = field(default_factory=dict)
    exit_reason: Optional[ExitReason] = None
    proposed_strike: Optional[int] = None
    proposed_entry_premium: Optional[float] = None
    proposed_stop_loss_pct: float = 0.40
    proposed_target_pct: float = 0.80
    engine_mode: Optional[str] = None
    decision_mode: Optional[str] = None
    decision_reason_code: Optional[str] = None
    decision_metrics: dict[str, Any] = field(default_factory=dict)
    strategy_family_version: Optional[str] = None
    strategy_profile_id: Optional[str] = None


@dataclass
class TradeSignal:
    """Final engine action emitted to downstream consumers."""

    signal_id: str
    timestamp: datetime
    snapshot_id: str
    signal_type: SignalType
    direction: Optional[str] = None
    strike: Optional[int] = None
    expiry: Optional[date] = None
    entry_premium: Optional[float] = None
    max_hold_bars: Optional[int] = None
    stop_loss_pct: float = 0.40
    target_pct: float = 0.80
    trailing_enabled: bool = False
    trailing_activation_pct: float = 0.10
    trailing_offset_pct: float = 0.05
    trailing_lock_breakeven: bool = True
    orb_trail_activation_mfe: float = 0.15
    orb_trail_offset_pct: float = 0.08
    orb_trail_min_lock_pct: float = 0.05
    orb_trail_priority_over_regime: bool = True
    orb_trail_regime_filter: Optional[str] = None
    oi_trail_activation_mfe: float = 0.15
    oi_trail_offset_pct: float = 0.08
    oi_trail_min_lock_pct: float = 0.05
    oi_trail_priority_over_regime: bool = True
    oi_trail_regime_filter: Optional[str] = None
    max_lots: int = 1
    position_id: Optional[str] = None
    entry_strategy_name: Optional[str] = None
    entry_regime_name: Optional[str] = None
    exit_reason: Optional[ExitReason] = None
    source: str = "RULE"
    confidence: Optional[float] = None
    reason: str = ""
    votes: list[StrategyVote] = field(default_factory=list)
    engine_mode: Optional[str] = None
    decision_mode: Optional[str] = None
    decision_reason_code: Optional[str] = None
    decision_metrics: dict[str, Any] = field(default_factory=dict)
    strategy_family_version: Optional[str] = None
    strategy_profile_id: Optional[str] = None


class StrategyEngine(ABC):
    """Engine contract invoked by the event bus consumer on every snapshot."""

    @abstractmethod
    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        """Evaluate one snapshot and optionally return a trade signal."""

    @abstractmethod
    def on_session_start(self, trade_date: date) -> None:
        """Session start hook."""

    @abstractmethod
    def on_session_end(self, trade_date: date) -> None:
        """Session end hook."""


class BaseStrategy(ABC):
    """Contract for internal pluggable deterministic strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier used in logs and backtests."""

    @abstractmethod
    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        """Evaluate the snapshot and optionally emit a vote."""

    def on_session_start(self, trade_date: date) -> None:
        """Reset any intraday state."""

    def on_session_end(self, trade_date: date) -> None:
        """Finalize any intraday state."""

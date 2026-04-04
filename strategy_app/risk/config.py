"""Replay and runtime risk configuration for strategy positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_positive_int(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


@dataclass(frozen=True)
class StrategyTrailConfig:
    activation_mfe: float = 0.15
    trail_offset: float = 0.08
    min_lock_pct: float = 0.05
    priority_over_regime: bool = True
    regime_filter: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Any) -> "StrategyTrailConfig":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            activation_mfe=max(0.0, float(_as_optional_float(payload.get("activation_mfe")) or 0.15)),
            trail_offset=max(0.0, float(_as_optional_float(payload.get("trail_offset")) or 0.08)),
            min_lock_pct=max(0.0, float(_as_optional_float(payload.get("min_lock_pct")) or 0.05)),
            priority_over_regime=_as_bool(payload.get("priority_over_regime"), default=True),
            regime_filter=(str(payload.get("regime_filter") or "").strip().upper() or None),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "activation_mfe": self.activation_mfe,
            "trail_offset": self.trail_offset,
            "min_lock_pct": self.min_lock_pct,
            "priority_over_regime": self.priority_over_regime,
            "regime_filter": self.regime_filter,
        }


@dataclass(frozen=True)
class PositionRiskConfig:
    stop_loss_pct: Optional[float] = None
    target_pct: Optional[float] = None
    trailing_enabled: bool = False
    trailing_activation_pct: float = 0.10
    trailing_offset_pct: float = 0.05
    trailing_lock_breakeven: bool = True
    orb_trail: StrategyTrailConfig = field(
        default_factory=lambda: StrategyTrailConfig(regime_filter="PRE_EXPIRY")
    )
    oi_trail: StrategyTrailConfig = field(
        default_factory=lambda: StrategyTrailConfig(regime_filter="PRE_EXPIRY")
    )
    regime_shift_confirm_bars: int = 2
    regime_shift_min_profit_hold_pct: Optional[float] = 0.08

    @classmethod
    def from_payload(cls, payload: Any) -> "PositionRiskConfig":
        if not isinstance(payload, dict):
            return cls()
        regime_shift_hold = _as_optional_float(payload.get("regime_shift_min_profit_hold_pct"))
        if regime_shift_hold is not None:
            regime_shift_hold = max(0.0, float(regime_shift_hold))
        return cls(
            stop_loss_pct=_as_optional_float(payload.get("stop_loss_pct")),
            target_pct=_as_optional_float(payload.get("target_pct")),
            trailing_enabled=_as_bool(payload.get("trailing_enabled"), default=False),
            trailing_activation_pct=max(0.0, float(_as_optional_float(payload.get("trailing_activation_pct")) or 0.10)),
            trailing_offset_pct=max(0.0, float(_as_optional_float(payload.get("trailing_offset_pct")) or 0.05)),
            trailing_lock_breakeven=_as_bool(payload.get("trailing_lock_breakeven"), default=True),
            orb_trail=StrategyTrailConfig.from_payload(payload.get("orb_trail")),
            oi_trail=StrategyTrailConfig.from_payload(payload.get("oi_trail")),
            regime_shift_confirm_bars=_as_positive_int(payload.get("regime_shift_confirm_bars"), default=2),
            regime_shift_min_profit_hold_pct=regime_shift_hold,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "target_pct": self.target_pct,
            "trailing_enabled": self.trailing_enabled,
            "trailing_activation_pct": self.trailing_activation_pct,
            "trailing_offset_pct": self.trailing_offset_pct,
            "trailing_lock_breakeven": self.trailing_lock_breakeven,
            "orb_trail": self.orb_trail.to_payload(),
            "oi_trail": self.oi_trail.to_payload(),
            "regime_shift_confirm_bars": self.regime_shift_confirm_bars,
            "regime_shift_min_profit_hold_pct": self.regime_shift_min_profit_hold_pct,
        }

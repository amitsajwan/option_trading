"""Replay and runtime risk configuration for strategy positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..utils.env import as_bool, as_optional_float, as_positive_int


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
            activation_mfe=max(0.0, float(as_optional_float(payload.get("activation_mfe")) or 0.15)),
            trail_offset=max(0.0, float(as_optional_float(payload.get("trail_offset")) or 0.08)),
            min_lock_pct=max(0.0, float(as_optional_float(payload.get("min_lock_pct")) or 0.05)),
            priority_over_regime=as_bool(payload.get("priority_over_regime"), default=True),
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

    # Underlying (futures) stop/target: fraction of entry futures price.
    # e.g. 0.002 = 100 pts on a 50,000 BankNifty futures.
    underlying_stop_pct: Optional[float] = None
    underlying_target_pct: Optional[float] = None

    # Stagnation exit: if after stagnant_exit_bars the trade hasn't reached
    # stagnant_min_gain_pct, exit early to stop theta decay.
    # Set stagnant_exit_bars=0 to disable.
    stagnant_exit_bars: int = 0
    stagnant_min_gain_pct: float = 0.05
    # "shadow_score_crossed_zero": also require momentum reversal before exiting.
    stagnant_exit_condition: str = ""

    # ORB wide-range filter: skip ORB entry if opening-range width exceeds
    # this many BankNifty points. Set 0 to disable.
    orb_max_range_pts: float = 0.0

    # Fast exit when ML 5m entry thesis fails early (bars ≈ minutes in replay).
    thesis_fail_exit_bars: int = 0
    thesis_fail_min_mfe_pct: float = 0.02
    thesis_fail_pnl_pct: float = -0.08
    early_stop_loss_bars: int = 0
    early_stop_loss_pct: Optional[float] = None
    atm_strike_only: bool = False
    allow_non_atm_for_ml_entry: bool = False

    @classmethod
    def from_payload(cls, payload: Any) -> "PositionRiskConfig":
        if not isinstance(payload, dict):
            return cls()
        regime_shift_hold = as_optional_float(payload.get("regime_shift_min_profit_hold_pct"))
        if regime_shift_hold is not None:
            regime_shift_hold = max(0.0, float(regime_shift_hold))
        return cls(
            stop_loss_pct=as_optional_float(payload.get("stop_loss_pct")),
            target_pct=as_optional_float(payload.get("target_pct")),
            trailing_enabled=as_bool(payload.get("trailing_enabled"), default=False),
            trailing_activation_pct=max(0.0, float(as_optional_float(payload.get("trailing_activation_pct")) or 0.10)),
            trailing_offset_pct=max(0.0, float(as_optional_float(payload.get("trailing_offset_pct")) or 0.05)),
            trailing_lock_breakeven=as_bool(payload.get("trailing_lock_breakeven"), default=True),
            orb_trail=StrategyTrailConfig.from_payload(payload.get("orb_trail")),
            oi_trail=StrategyTrailConfig.from_payload(payload.get("oi_trail")),
            regime_shift_confirm_bars=as_positive_int(payload.get("regime_shift_confirm_bars"), default=2),
            regime_shift_min_profit_hold_pct=regime_shift_hold,
            underlying_stop_pct=as_optional_float(payload.get("underlying_stop_pct")),
            underlying_target_pct=as_optional_float(payload.get("underlying_target_pct")),
            stagnant_exit_bars=max(0, int(as_optional_float(payload.get("stagnant_exit_bars")) or 0)),
            stagnant_min_gain_pct=max(0.0, float(as_optional_float(payload.get("stagnant_min_gain_pct")) or 0.05)),
            stagnant_exit_condition=str(payload.get("stagnant_exit_condition") or ""),
            orb_max_range_pts=max(0.0, float(as_optional_float(payload.get("orb_max_range_pts")) or 0.0)),
            thesis_fail_exit_bars=max(0, int(as_optional_float(payload.get("thesis_fail_exit_bars")) or 0)),
            thesis_fail_min_mfe_pct=max(0.0, float(as_optional_float(payload.get("thesis_fail_min_mfe_pct")) or 0.02)),
            thesis_fail_pnl_pct=float(as_optional_float(payload.get("thesis_fail_pnl_pct")) or -0.08),
            early_stop_loss_bars=max(0, int(as_optional_float(payload.get("early_stop_loss_bars")) or 0)),
            early_stop_loss_pct=as_optional_float(payload.get("early_stop_loss_pct")),
            atm_strike_only=as_bool(payload.get("atm_strike_only"), default=False),
            allow_non_atm_for_ml_entry=as_bool(payload.get("allow_non_atm_for_ml_entry"), default=False),
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
            "underlying_stop_pct": self.underlying_stop_pct,
            "underlying_target_pct": self.underlying_target_pct,
            "stagnant_exit_bars": self.stagnant_exit_bars,
            "stagnant_min_gain_pct": self.stagnant_min_gain_pct,
            "stagnant_exit_condition": self.stagnant_exit_condition,
            "orb_max_range_pts": self.orb_max_range_pts,
            "thesis_fail_exit_bars": self.thesis_fail_exit_bars,
            "thesis_fail_min_mfe_pct": self.thesis_fail_min_mfe_pct,
            "thesis_fail_pnl_pct": self.thesis_fail_pnl_pct,
            "early_stop_loss_bars": self.early_stop_loss_bars,
            "early_stop_loss_pct": self.early_stop_loss_pct,
            "atm_strike_only": self.atm_strike_only,
            "allow_non_atm_for_ml_entry": self.allow_non_atm_for_ml_entry,
        }

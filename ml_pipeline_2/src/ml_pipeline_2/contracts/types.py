from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Tuple


FEATURE_PROFILE_ALL = "all"
FEATURE_PROFILE_FUTURES_OPTIONS_ONLY = "futures_options_only"
FEATURE_PROFILE_CORE_V1 = "core_v1"
FEATURE_PROFILE_CORE_V2 = "core_v2"
FEATURE_PROFILE_FUTURES_CORE = "futures_core"
FEATURE_PROFILES: Tuple[str, ...] = (
    FEATURE_PROFILE_ALL,
    FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    FEATURE_PROFILE_CORE_V1,
    FEATURE_PROFILE_CORE_V2,
    FEATURE_PROFILE_FUTURES_CORE,
)

LABEL_TARGET_BASE = "base_label"
LABEL_TARGET_PATH_TP_SL = "path_tp_sl"
LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO = "path_tp_sl_time_stop_zero"
LABEL_TARGET_PATH_TP_SL_RESOLVED_ONLY = "path_tp_sl_resolved_only"
LABEL_TARGET_MOVE_BARRIER_HIT = "move_barrier_hit"
LABEL_TARGET_MOVE_DIRECTION_UP = "move_direction_up"
LABEL_TARGET_CHOICES: Tuple[str, ...] = (
    LABEL_TARGET_BASE,
    LABEL_TARGET_PATH_TP_SL,
    LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO,
    LABEL_TARGET_PATH_TP_SL_RESOLVED_ONLY,
    LABEL_TARGET_MOVE_BARRIER_HIT,
    LABEL_TARGET_MOVE_DIRECTION_UP,
)


@dataclass(frozen=True)
class DateWindow:
    start: str
    end: str

    def to_dict(self) -> Dict[str, str]:
        return {"start": str(self.start), "end": str(self.end)}


@dataclass(frozen=True)
class FeatureSetSpec:
    name: str
    exclude_regex: Tuple[str, ...] = ()
    include_regex: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    family: str
    params: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreprocessConfig:
    max_missing_rate: float = 0.35
    clip_lower_q: float = 0.01
    clip_upper_q: float = 0.99

    def to_dict(self) -> Dict[str, float]:
        return {
            "max_missing_rate": float(self.max_missing_rate),
            "clip_lower_q": float(self.clip_lower_q),
            "clip_upper_q": float(self.clip_upper_q),
        }


@dataclass(frozen=True)
class TradingObjectiveConfig:
    ce_threshold: float = 0.60
    pe_threshold: float = 0.60
    cost_per_trade: float = 0.0006
    min_profit_factor: float = 1.30
    max_equity_drawdown_pct: float = 0.15
    min_trades: int = 50
    take_profit_pct: float = 0.003
    stop_loss_pct: float = 0.001
    discard_time_stop: bool = False
    risk_per_trade_pct: float = 0.01

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ce_threshold": float(self.ce_threshold),
            "pe_threshold": float(self.pe_threshold),
            "cost_per_trade": float(self.cost_per_trade),
            "min_profit_factor": float(self.min_profit_factor),
            "max_equity_drawdown_pct": float(self.max_equity_drawdown_pct),
            "min_trades": int(self.min_trades),
            "take_profit_pct": float(self.take_profit_pct),
            "stop_loss_pct": float(self.stop_loss_pct),
            "discard_time_stop": bool(self.discard_time_stop),
            "risk_per_trade_pct": float(self.risk_per_trade_pct),
        }


@dataclass(frozen=True)
class LabelRecipe:
    recipe_id: str
    horizon_minutes: int
    take_profit_pct: float
    stop_loss_pct: float
    risk_basis: str = "underlying"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recipe_id": str(self.recipe_id),
            "horizon_minutes": int(self.horizon_minutes),
            "take_profit_pct": float(self.take_profit_pct),
            "stop_loss_pct": float(self.stop_loss_pct),
            "risk_basis": str(self.risk_basis),
        }

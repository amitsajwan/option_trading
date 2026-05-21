from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple, Union


@dataclass(frozen=True)
class Condition:
    column: str
    operator: str  # '>', '<', '>=', '<=', '==', '!='
    value: Union[float, str]  # literal value, or column name for cross-column compare


@dataclass(frozen=True)
class TradeScoreConfig:
    """How to rank candidate entry rows when applying max_trades_per_day."""

    mode: str = "first"  # "first" | "columns"
    columns: Tuple[str, ...] = ()
    weights: Tuple[float, ...] = ()
    use_abs: bool = True


@dataclass(frozen=True)
class ExitConfig:
    stop_pct: float
    target_pct: float
    time_stop_minutes: int
    eod_force_close_minute: int
    signal_exits: Tuple[Condition, ...] = ()  # empty = mechanical only
    trail_activation_pct: Optional[float] = None  # MFE (pnl fraction) to arm trail
    trail_giveback_pct: Optional[float] = None  # exit if pnl falls this much below peak MFE
    underlying_stop_pct: Optional[float] = None  # adverse futures move vs entry (e.g. 0.003)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    direction: str  # 'BUY_ATM_CE' | 'BUY_ATM_PE'
    entry_conditions: Tuple[Condition, ...]
    disqualifiers: Tuple[Condition, ...]
    exit_mechanical: ExitConfig
    exit_signal: Optional[ExitConfig] = None
    disqualifier_all_of: Tuple[Tuple[Condition, ...], ...] = ()  # any group fully true → block
    max_trades_per_day: Optional[int] = None
    trade_score: Optional[TradeScoreConfig] = None

    @classmethod
    def _parse_trade_score(cls, raw: Optional[Dict[str, Any]]) -> Optional[TradeScoreConfig]:
        if not raw:
            return None
        mode = str(raw.get("mode", "columns")).strip().lower()
        cols = tuple(str(c) for c in raw.get("columns", ()))
        weights_raw = raw.get("weights", ())
        weights = tuple(float(w) for w in weights_raw) if weights_raw else ()
        if weights and len(weights) != len(cols):
            raise ValueError("trade_score weights length must match columns")
        return TradeScoreConfig(
            mode=mode,
            columns=cols,
            weights=weights,
            use_abs=bool(raw.get("abs", True)),
        )

    @classmethod
    def _parse_exit_config(cls, raw: Dict[str, Any]) -> ExitConfig:
        signal_exits = tuple(Condition(**c) for c in raw.get("signal_exits", []))
        base_keys = {
            "stop_pct", "target_pct", "time_stop_minutes", "eod_force_close_minute",
            "trail_activation_pct", "trail_giveback_pct", "underlying_stop_pct",
        }
        kwargs = {k: v for k, v in raw.items() if k in base_keys}
        return ExitConfig(**kwargs, signal_exits=signal_exits)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Rule":
        entry = tuple(Condition(**c) for c in d["entry_conditions"])
        disqualifiers = tuple(Condition(**c) for c in d.get("disqualifiers", []))
        disqualifier_all_of = tuple(
            tuple(Condition(**c) for c in group)
            for group in d.get("disqualifier_all_of", [])
        )

        exit_mech = cls._parse_exit_config(d["exit_mechanical"])

        exit_sig = None
        if "exit_signal" in d and d["exit_signal"] is not None:
            exit_sig = cls._parse_exit_config(d["exit_signal"])

        max_trades = d.get("max_trades_per_day")
        max_trades_per_day = int(max_trades) if max_trades is not None else None

        return cls(
            rule_id=d["rule_id"],
            direction=d["direction"],
            entry_conditions=entry,
            disqualifiers=disqualifiers,
            disqualifier_all_of=disqualifier_all_of,
            exit_mechanical=exit_mech,
            exit_signal=exit_sig,
            max_trades_per_day=max_trades_per_day,
            trade_score=cls._parse_trade_score(d.get("trade_score")),
        )

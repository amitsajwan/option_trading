from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple, Union


@dataclass(frozen=True)
class Condition:
    column: str
    operator: str  # '>', '<', '>=', '<=', '==', '!='
    value: Union[float, str]  # literal value, or column name for cross-column compare


@dataclass(frozen=True)
class ExitConfig:
    stop_pct: float
    target_pct: float
    time_stop_minutes: int
    eod_force_close_minute: int
    signal_exits: Tuple[Condition, ...] = ()  # empty = mechanical only


@dataclass(frozen=True)
class Rule:
    rule_id: str
    direction: str  # 'BUY_ATM_CE' | 'BUY_ATM_PE'
    entry_conditions: Tuple[Condition, ...]
    disqualifiers: Tuple[Condition, ...]
    exit_mechanical: ExitConfig
    exit_signal: Optional[ExitConfig] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Rule":
        entry = tuple(Condition(**c) for c in d["entry_conditions"])
        disqualifiers = tuple(Condition(**c) for c in d.get("disqualifiers", []))

        exit_mech = ExitConfig(
            **{k: v for k, v in d["exit_mechanical"].items() if k != "signal_exits"},
            signal_exits=tuple(Condition(**c) for c in d["exit_mechanical"].get("signal_exits", [])),
        )

        exit_sig = None
        if "exit_signal" in d and d["exit_signal"] is not None:
            exit_sig = ExitConfig(
                **{k: v for k, v in d["exit_signal"].items() if k != "signal_exits"},
                signal_exits=tuple(Condition(**c) for c in d["exit_signal"].get("signal_exits", [])),
            )

        return cls(
            rule_id=d["rule_id"],
            direction=d["direction"],
            entry_conditions=entry,
            disqualifiers=disqualifiers,
            exit_mechanical=exit_mech,
            exit_signal=exit_sig,
        )

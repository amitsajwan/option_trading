"""R1S top-3/day short ATM CE — paper runtime strategy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from ...contracts import (
    BaseStrategy,
    Direction,
    ExitReason,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyVote,
)
from ..r1s_rule_runtime import composite_score, default_s3_rule, row_passes_entry
from ..snapshot_accessor import SnapshotAccessor


@dataclass(frozen=True)
class _Candidate:
    minute: float
    score: float


class R1sTop3ShortCeStrategy(BaseStrategy):
    """Sell ATM CE on ORB-down fade; max 3 ranked entries per session day."""

    name = "R1S_TOP3_SHORT_CE"

    def __init__(self, *, rule_path: Optional[str] = None) -> None:
        if rule_path is None:
            self._rule = default_s3_rule()
        else:
            from ..r1s_rule_runtime import load_rule

            self._rule = load_rule(rule_path)
        self._rule_path = rule_path
        self._trade_date: Optional[str] = None
        self._candidates: list[_Candidate] = []
        self._entries_taken = 0

    def on_session_start(self, trade_date: date) -> None:
        self._trade_date = trade_date.isoformat()
        self._candidates = []
        self._entries_taken = 0

    def on_session_end(self, trade_date: date) -> None:
        self.on_session_start(trade_date)

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        snap = SnapshotAccessor(snapshot)
        rule = self._rule

        if position is not None:
            return None

        if self._entries_taken >= int(rule.max_trades_per_day or 3):
            return None

        if not row_passes_entry(snap, rule):
            return None

        score_cfg = rule.trade_score
        minute = float(snap.minutes_since_open or 0)
        score = composite_score(snap, score_cfg) if score_cfg is not None else -minute
        self._candidates.append(_Candidate(minute=minute, score=score))

        ranked = sorted(self._candidates, key=lambda c: (-c.score, c.minute))
        top = ranked[: int(rule.max_trades_per_day or 3)]
        if minute not in {c.minute for c in top}:
            return None

        premium = snap.atm_ce_close
        strike = snap.atm_strike
        if premium is None or premium <= 0 or strike is None or int(strike) <= 0:
            return None

        self._entries_taken += 1
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=Direction.CE,
            confidence=0.85,
            reason="R1S_TOP3_S3: ORB-down fade short CE",
            raw_signals={
                "_entry_policy_mode": "bypass",
                "_r1s_short_ce": True,
                "_r1s_top3_score": round(score, 6),
                "_r1s_top3_rank_slot": self._entries_taken,
            },
            proposed_strike=int(strike),
            proposed_entry_premium=float(premium),
            proposed_stop_loss_pct=1.0,
            proposed_target_pct=0.5,
        )

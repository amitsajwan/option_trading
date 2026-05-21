"""Top-3/day rule strategies that buy ATM CE or PE (debit / long premium only)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from ...contracts import (
    BaseStrategy,
    Direction,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyVote,
)
from ..r1s_rule_runtime import (
    composite_score,
    direction_from_rule,
    load_rule,
    row_passes_entry,
)
from ..snapshot_accessor import SnapshotAccessor
from ml_pipeline_2.scripts.rules_pipeline.rule_schema import Rule

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_R1_LONG_PE_RULE = (
    _REPO_ROOT / "ml_pipeline_2/configs/rules/debit_multi/r1_top3_long_pe_s3.json"
)
DEFAULT_R2_LONG_CE_RULE = (
    _REPO_ROOT / "ml_pipeline_2/configs/rules/debit_multi/r2_top3_long_ce_s3.json"
)


@dataclass(frozen=True)
class _Candidate:
    minute: float
    score: float


class RuleTop3LongOptionStrategy(BaseStrategy):
    """Buy ATM CE or PE from rules JSON; max N ranked entries per session day."""

    def __init__(
        self,
        *,
        rule_path: Optional[str] = None,
        default_rule_path: Optional[Path] = None,
    ) -> None:
        path = rule_path or (str(default_rule_path) if default_rule_path else None)
        if path is None:
            raise ValueError("rule_path or default_rule_path is required")
        self._rule = load_rule(path)
        self._rule_path = path
        self._direction = direction_from_rule(self._rule)
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
        del risk
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

        premium, strike = self._entry_premium_and_strike(snap)
        if premium is None or premium <= 0 or strike is None or int(strike) <= 0:
            return None

        exit_cfg = rule.exit_mechanical
        self._entries_taken += 1
        leg = "CE" if self._direction == Direction.CE else "PE"
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=self._direction,
            confidence=0.85,
            reason=f"{rule.rule_id}: long ATM {leg}",
            raw_signals={
                "_entry_policy_mode": "bypass",
                "_debit_long_option": True,
                "_rule_id": rule.rule_id,
                "_r1s_top3_score": round(score, 6),
                "_r1s_top3_rank_slot": self._entries_taken,
            },
            proposed_strike=int(strike),
            proposed_entry_premium=float(premium),
            proposed_stop_loss_pct=float(exit_cfg.stop_pct) / 100.0,
            proposed_target_pct=float(exit_cfg.target_pct) / 100.0,
        )

    def _entry_premium_and_strike(
        self, snap: SnapshotAccessor
    ) -> tuple[Optional[float], Optional[int]]:
        strike = snap.atm_strike
        if self._direction == Direction.CE:
            return snap.atm_ce_close, int(strike) if strike is not None else None
        return snap.atm_pe_close, int(strike) if strike is not None else None


class R1Top3LongPeStrategy(RuleTop3LongOptionStrategy):
    name = "R1_TOP3_LONG_PE"

    def __init__(self, *, rule_path: Optional[str] = None) -> None:
        super().__init__(rule_path=rule_path, default_rule_path=DEFAULT_R1_LONG_PE_RULE)


class R2Top3LongCeStrategy(RuleTop3LongOptionStrategy):
    name = "R2_TOP3_LONG_CE"

    def __init__(self, *, rule_path: Optional[str] = None) -> None:
        super().__init__(rule_path=rule_path, default_rule_path=DEFAULT_R2_LONG_CE_RULE)

"""Top-3/day short ATM CE from rules JSON — Playbook v1 runtime."""
from __future__ import annotations

import os
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
from ..playbook_brain import (
    PLAYBOOK_EXIT_KEY,
    is_short_rule,
    playbook_exit_metrics,
    vote_exit_fractions,
)
from ..r1s_rule_runtime import composite_score, load_rule, row_passes_entry
from ..snapshot_accessor import SnapshotAccessor
from ml_pipeline_2.scripts.rules_pipeline.rule_schema import Rule

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PLAYBOOK_RULE = (
    _REPO_ROOT
    / "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_thesis.json"
)


@dataclass(frozen=True)
class _Candidate:
    minute: float
    score: float


class RuleTop3ShortCeStrategy(BaseStrategy):
    """Sell ATM CE from rules JSON; exits delegated to PlaybookBrain in tracker."""

    def __init__(
        self,
        *,
        rule_path: Optional[str] = None,
        default_rule_path: Optional[Path] = None,
        strategy_name: str = "RULE_TOP3_SHORT_CE",
    ) -> None:
        path = rule_path or (str(default_rule_path) if default_rule_path else None)
        if path is None:
            raise ValueError("rule_path or default_rule_path is required")
        self._rule = load_rule(path)
        if not is_short_rule(self._rule):
            raise ValueError(f"rule {self._rule.rule_id} is not a short premium direction")
        self._rule_path = path
        self.name = strategy_name
        self._candidates: list[_Candidate] = []
        self._entries_taken = 0

    def on_session_start(self, trade_date: date) -> None:
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

        premium = snap.atm_ce_close
        strike = snap.atm_strike
        if premium is None or premium <= 0 or strike is None or int(strike) <= 0:
            return None

        exit_cfg = rule.exit_mechanical
        stop_pct, target_pct, underlying_stop, max_hold = vote_exit_fractions(exit_cfg)
        self._entries_taken += 1
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=Direction.CE,
            confidence=0.88,
            reason=f"{rule.rule_id}: ORB-down fade short CE (playbook)",
            raw_signals={
                "_entry_policy_mode": "bypass",
                "_playbook_brain": True,
                "_rule_id": rule.rule_id,
                "_r1s_top3_score": round(score, 6),
                "_r1s_top3_rank_slot": self._entries_taken,
                PLAYBOOK_EXIT_KEY: playbook_exit_metrics(rule),
                "_max_hold_bars": max_hold,
                "_underlying_stop_pct": underlying_stop,
            },
            proposed_strike=int(strike),
            proposed_entry_premium=float(premium),
            proposed_stop_loss_pct=stop_pct,
            proposed_target_pct=target_pct,
        )


def _resolve_playbook_rule_path() -> Path:
    override = str(os.getenv("PLAYBOOK_V1_RULE_PATH") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_PLAYBOOK_RULE


class PlaybookV1ShortCeStrategy(RuleTop3ShortCeStrategy):
    """Monthly winner (2026-05): thesis exit; profile playbook_v1_paper_v1."""

    name = "PBV1_TOP3_THESIS"

    def __init__(self, *, rule_path: Optional[str] = None) -> None:
        super().__init__(
            rule_path=rule_path,
            default_rule_path=_resolve_playbook_rule_path(),
            strategy_name=self.name,
        )

"""Append-only JSONL audit logs for strategy votes and signals."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from contracts_app import (
    build_strategy_position_event,
    build_strategy_vote_event,
    build_trade_signal_event,
    strategy_position_topic,
    strategy_vote_topic,
    trade_signal_topic,
)

from ..contracts import PositionContext, StrategyVote, TradeSignal
from .decision_field_resolver import DecisionFieldResolver
from .jsonl_sink import append_jsonl, normalize_record_timestamps
from .redis_event_publisher import RedisEventPublisher

logger = logging.getLogger(__name__)

class SignalLogger:
    """Writes votes, signals, and position lifecycle events to JSONL."""

    def __init__(self, run_dir: Optional[Path] = None) -> None:
        base_dir = Path(run_dir) if run_dir else Path(os.getenv("STRATEGY_RUN_DIR", ".run/strategy_app"))
        self._votes_path = base_dir / "votes.jsonl"
        self._signals_path = base_dir / "signals.jsonl"
        self._positions_path = base_dir / "positions.jsonl"
        self._resolver = DecisionFieldResolver()
        self._publisher = RedisEventPublisher(logger=logger)
        self._run_id: Optional[str] = None

    def set_run_context(self, run_id: Optional[str], metadata: Optional[dict[str, Any]] = None) -> None:
        text = str(run_id or "").strip()
        self._run_id = text or None
        self._resolver.update_context(metadata)

    def _metadata(self, **extra: Any) -> dict[str, Any]:
        return self._resolver.metadata(run_id=self._run_id, **extra)

    def _publish(self, topic: str, event: dict[str, Any]) -> None:
        self._publisher.publish(topic, event)

    def _effective_engine_mode(self, explicit: Any, *, source: Any = None) -> str:
        return self._resolver.effective_engine_mode(explicit, source=source)

    def _resolve_decision_mode_for_vote(self, vote: StrategyVote, engine_mode: str) -> str:
        return self._resolver.resolve_decision_mode_for_vote(vote, engine_mode)

    def _resolve_decision_mode_for_signal(self, signal: TradeSignal, engine_mode: str) -> str:
        return self._resolver.resolve_decision_mode_for_signal(signal, engine_mode)

    def _resolve_reason_code_for_vote(self, vote: StrategyVote) -> Optional[str]:
        return self._resolver.resolve_reason_code_for_vote(vote)

    def _resolve_reason_code_for_signal(self, signal: TradeSignal) -> Optional[str]:
        return self._resolver.resolve_reason_code_for_signal(signal)

    def _resolve_strategy_family_version(
        self,
        *,
        explicit: Any,
        engine_mode: str,
        decision_mode: str,
    ) -> str:
        return self._resolver.resolve_strategy_family_version(
            explicit=explicit,
            engine_mode=engine_mode,
            decision_mode=decision_mode,
        )

    def _resolve_strategy_profile_id(self, *, explicit: Any, engine_mode: str) -> str:
        return self._resolver.resolve_strategy_profile_id(explicit=explicit, engine_mode=engine_mode)

    def _vote_decision_metrics(self, vote: StrategyVote) -> dict[str, float]:
        return self._resolver.vote_decision_metrics(vote)

    def _signal_decision_metrics(self, signal: TradeSignal) -> dict[str, float]:
        return self._resolver.signal_decision_metrics(signal)

    def _vote_record(self, vote: StrategyVote) -> dict[str, Any]:
        regime = vote.raw_signals.get("_regime") if isinstance(vote.raw_signals, dict) else None
        regime_conf = vote.raw_signals.get("_regime_conf") if isinstance(vote.raw_signals, dict) else None
        regime_reason = vote.raw_signals.get("_regime_reason") if isinstance(vote.raw_signals, dict) else None
        engine_mode = self._effective_engine_mode(vote.engine_mode)
        decision_mode = self._resolve_decision_mode_for_vote(vote, engine_mode)
        return {
            "event": "VOTE",
            "strategy": vote.strategy_name,
            "snapshot_id": vote.snapshot_id,
            "timestamp": vote.timestamp,
            "trade_date": vote.trade_date,
            "regime": regime,
            "regime_conf": regime_conf,
            "regime_reason": regime_reason,
            "signal_type": vote.signal_type.value if vote.signal_type else None,
            "direction": vote.direction.value if vote.direction else None,
            "confidence": vote.confidence,
            "reason": vote.reason,
            "exit_reason": vote.exit_reason.value if vote.exit_reason else None,
            "proposed_strike": vote.proposed_strike,
            "proposed_entry_premium": vote.proposed_entry_premium,
            "proposed_stop_loss_pct": vote.proposed_stop_loss_pct,
            "proposed_target_pct": vote.proposed_target_pct,
            "engine_mode": engine_mode,
            "decision_mode": decision_mode,
            "decision_reason_code": self._resolve_reason_code_for_vote(vote),
            "decision_metrics": (self._vote_decision_metrics(vote) or None),
            "strategy_family_version": self._resolve_strategy_family_version(
                explicit=vote.strategy_family_version,
                engine_mode=engine_mode,
                decision_mode=decision_mode,
            ),
            "strategy_profile_id": self._resolve_strategy_profile_id(
                explicit=vote.strategy_profile_id,
                engine_mode=engine_mode,
            ),
            "raw_signals": vote.raw_signals,
            "run_id": self._run_id,
        }

    def _signal_record(self, signal: TradeSignal, *, acted_on: bool) -> dict[str, Any]:
        regime = None
        regime_conf = None
        for vote in signal.votes:
            if isinstance(vote.raw_signals, dict) and vote.raw_signals.get("_regime"):
                regime = vote.raw_signals.get("_regime")
                regime_conf = vote.raw_signals.get("_regime_conf")
                break
        engine_mode = self._effective_engine_mode(signal.engine_mode, source=signal.source)
        decision_mode = self._resolve_decision_mode_for_signal(signal, engine_mode)
        return {
            "event": "SIGNAL",
            "signal_id": signal.signal_id,
            "timestamp": signal.timestamp,
            "snapshot_id": signal.snapshot_id,
            "regime": regime,
            "regime_conf": regime_conf,
            "signal_type": signal.signal_type.value if signal.signal_type else None,
            "direction": signal.direction,
            "strike": signal.strike,
            "entry_premium": signal.entry_premium,
            "max_hold_bars": signal.max_hold_bars,
            "stop_loss_pct": signal.stop_loss_pct,
            "target_pct": signal.target_pct,
            "trailing_enabled": signal.trailing_enabled,
            "trailing_activation_pct": signal.trailing_activation_pct,
            "trailing_offset_pct": signal.trailing_offset_pct,
            "trailing_lock_breakeven": signal.trailing_lock_breakeven,
            "orb_trail_activation_mfe": signal.orb_trail_activation_mfe,
            "orb_trail_offset_pct": signal.orb_trail_offset_pct,
            "orb_trail_min_lock_pct": signal.orb_trail_min_lock_pct,
            "orb_trail_priority_over_regime": signal.orb_trail_priority_over_regime,
            "orb_trail_regime_filter": signal.orb_trail_regime_filter,
            "oi_trail_activation_mfe": signal.oi_trail_activation_mfe,
            "oi_trail_offset_pct": signal.oi_trail_offset_pct,
            "oi_trail_min_lock_pct": signal.oi_trail_min_lock_pct,
            "oi_trail_priority_over_regime": signal.oi_trail_priority_over_regime,
            "oi_trail_regime_filter": signal.oi_trail_regime_filter,
            "max_lots": signal.max_lots,
            "position_id": signal.position_id,
            "entry_strategy_name": signal.entry_strategy_name,
            "entry_regime_name": signal.entry_regime_name,
            "exit_reason": signal.exit_reason.value if signal.exit_reason else None,
            "source": signal.source,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "engine_mode": engine_mode,
            "decision_mode": decision_mode,
            "decision_reason_code": self._resolve_reason_code_for_signal(signal),
            "decision_metrics": (self._signal_decision_metrics(signal) or None),
            "strategy_family_version": self._resolve_strategy_family_version(
                explicit=signal.strategy_family_version,
                engine_mode=engine_mode,
                decision_mode=decision_mode,
            ),
            "strategy_profile_id": self._resolve_strategy_profile_id(
                explicit=signal.strategy_profile_id,
                engine_mode=engine_mode,
            ),
            "acted_on": acted_on,
            "contributing_strategies": [vote.strategy_name for vote in signal.votes],
            "run_id": self._run_id,
        }

    def _position_contract_fields(self, signal: Optional[TradeSignal] = None) -> dict[str, Any]:
        engine_mode = self._effective_engine_mode(
            getattr(signal, "engine_mode", None),
            source=(getattr(signal, "source", None) if signal is not None else None),
        )
        if signal is not None:
            decision_mode = self._resolve_decision_mode_for_signal(signal, engine_mode)
            reason_code = self._resolve_reason_code_for_signal(signal)
        else:
            decision_mode = "rule_vote"
            reason_code = None
        return {
            "engine_mode": engine_mode,
            "decision_mode": decision_mode,
            "decision_reason_code": reason_code,
            "strategy_family_version": self._resolve_strategy_family_version(
                explicit=(getattr(signal, "strategy_family_version", None) if signal is not None else None),
                engine_mode=engine_mode,
                decision_mode=decision_mode,
            ),
            "strategy_profile_id": self._resolve_strategy_profile_id(
                explicit=(getattr(signal, "strategy_profile_id", None) if signal is not None else None),
                engine_mode=engine_mode,
            ),
        }

    def log_vote(self, vote: StrategyVote) -> None:
        record = normalize_record_timestamps(self._vote_record(vote))
        append_jsonl(self._votes_path, record, logger=logger)
        self._publish(
            strategy_vote_topic(),
            build_strategy_vote_event(
                vote=record,
                source="strategy_app",
                metadata=self._metadata(strategy=vote.strategy_name, snapshot_id=vote.snapshot_id),
            ),
        )

    def log_signal(self, signal: TradeSignal, *, acted_on: bool = True) -> None:
        record = normalize_record_timestamps(self._signal_record(signal, acted_on=acted_on))
        append_jsonl(self._signals_path, record, logger=logger)
        self._publish(
            trade_signal_topic(),
            build_trade_signal_event(
                signal=record,
                source="strategy_app",
                metadata=self._metadata(signal_id=signal.signal_id, snapshot_id=signal.snapshot_id),
            ),
        )

    def log_position_open(self, signal: TradeSignal, position: PositionContext) -> None:
        record = normalize_record_timestamps({
            "event": "POSITION_OPEN",
            "position_id": position.position_id,
            "signal_id": signal.signal_id,
            "timestamp": signal.timestamp,
            "direction": position.direction,
            "strike": position.strike,
            "entry_premium": position.entry_premium,
            "lots": position.lots,
            "max_hold_bars": position.max_hold_bars,
            "entry_strategy": position.entry_strategy,
            "entry_regime": position.entry_regime,
            "stop_loss_pct": position.stop_loss_pct,
            "stop_price": position.stop_price,
            "high_water_premium": position.high_water_premium,
            "trailing_enabled": position.trailing_enabled,
            "trailing_activation_pct": position.trailing_activation_pct,
            "trailing_offset_pct": position.trailing_offset_pct,
            "trailing_lock_breakeven": position.trailing_lock_breakeven,
            "trailing_active": position.trailing_active,
            "orb_trail_activation_mfe": position.orb_trail_activation_mfe,
            "orb_trail_offset_pct": position.orb_trail_offset_pct,
            "orb_trail_min_lock_pct": position.orb_trail_min_lock_pct,
            "orb_trail_priority_over_regime": position.orb_trail_priority_over_regime,
            "orb_trail_regime_filter": position.orb_trail_regime_filter,
            "orb_trail_active": position.orb_trail_active,
            "orb_trail_stop_price": position.orb_trail_stop_price,
            "oi_trail_activation_mfe": position.oi_trail_activation_mfe,
            "oi_trail_offset_pct": position.oi_trail_offset_pct,
            "oi_trail_min_lock_pct": position.oi_trail_min_lock_pct,
            "oi_trail_priority_over_regime": position.oi_trail_priority_over_regime,
            "oi_trail_regime_filter": position.oi_trail_regime_filter,
            "oi_trail_active": position.oi_trail_active,
            "oi_trail_stop_price": position.oi_trail_stop_price,
            "target_pct": position.target_pct,
            "reason": signal.reason,
            "run_id": self._run_id,
            **self._position_contract_fields(signal),
        })
        append_jsonl(self._positions_path, record, logger=logger)
        self._publish(
            strategy_position_topic(),
            build_strategy_position_event(
                position=record,
                source="strategy_app",
                metadata=self._metadata(position_id=position.position_id, signal_id=signal.signal_id),
            ),
        )

    def log_position_manage(self, *, position: PositionContext, timestamp: datetime, snapshot_id: str) -> None:
        record = normalize_record_timestamps({
            "event": "POSITION_MANAGE",
            "position_id": position.position_id,
            "timestamp": timestamp,
            "snapshot_id": snapshot_id,
            "direction": position.direction,
            "strike": position.strike,
            "current_premium": position.current_premium,
            "pnl_pct": position.pnl_pct,
            "bars_held": position.bars_held,
            "max_hold_bars": position.max_hold_bars,
            "stop_loss_pct": position.stop_loss_pct,
            "stop_price": position.stop_price,
            "high_water_premium": position.high_water_premium,
            "target_pct": position.target_pct,
            "trailing_enabled": position.trailing_enabled,
            "trailing_activation_pct": position.trailing_activation_pct,
            "trailing_offset_pct": position.trailing_offset_pct,
            "trailing_lock_breakeven": position.trailing_lock_breakeven,
            "trailing_active": position.trailing_active,
            "orb_trail_activation_mfe": position.orb_trail_activation_mfe,
            "orb_trail_offset_pct": position.orb_trail_offset_pct,
            "orb_trail_min_lock_pct": position.orb_trail_min_lock_pct,
            "orb_trail_priority_over_regime": position.orb_trail_priority_over_regime,
            "orb_trail_regime_filter": position.orb_trail_regime_filter,
            "orb_trail_active": position.orb_trail_active,
            "orb_trail_stop_price": position.orb_trail_stop_price,
            "oi_trail_activation_mfe": position.oi_trail_activation_mfe,
            "oi_trail_offset_pct": position.oi_trail_offset_pct,
            "oi_trail_min_lock_pct": position.oi_trail_min_lock_pct,
            "oi_trail_priority_over_regime": position.oi_trail_priority_over_regime,
            "oi_trail_regime_filter": position.oi_trail_regime_filter,
            "oi_trail_active": position.oi_trail_active,
            "oi_trail_stop_price": position.oi_trail_stop_price,
            "run_id": self._run_id,
            **self._position_contract_fields(),
        })
        append_jsonl(self._positions_path, record, logger=logger)
        self._publish(
            strategy_position_topic(),
            build_strategy_position_event(
                position=record,
                source="strategy_app",
                metadata=self._metadata(position_id=position.position_id, snapshot_id=snapshot_id),
            ),
        )

    def log_position_close(
        self,
        *,
        exit_signal: TradeSignal,
        entry_premium: float,
        exit_premium: float,
        pnl_pct: float,
        mfe_pct: float,
        mae_pct: float,
        bars_held: int,
        stop_loss_pct: float,
        stop_price: Optional[float],
        high_water_premium: float,
        target_pct: float,
        trailing_enabled: bool,
        trailing_activation_pct: float,
        trailing_offset_pct: float,
        trailing_lock_breakeven: bool,
        trailing_active: bool,
        orb_trail_activation_mfe: float,
        orb_trail_offset_pct: float,
        orb_trail_min_lock_pct: float,
        orb_trail_priority_over_regime: bool,
        orb_trail_regime_filter: Optional[str],
        orb_trail_active: bool,
        orb_trail_stop_price: Optional[float],
        oi_trail_activation_mfe: float,
        oi_trail_offset_pct: float,
        oi_trail_min_lock_pct: float,
        oi_trail_priority_over_regime: bool,
        oi_trail_regime_filter: Optional[str],
        oi_trail_active: bool,
        oi_trail_stop_price: Optional[float],
    ) -> None:
        record = normalize_record_timestamps({
            "event": "POSITION_CLOSE",
            "position_id": exit_signal.position_id,
            "timestamp": exit_signal.timestamp,
            "direction": exit_signal.direction,
            "strike": exit_signal.strike,
            "entry_premium": entry_premium,
            "exit_premium": exit_premium,
            "pnl_pct": pnl_pct,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "bars_held": bars_held,
            "stop_loss_pct": stop_loss_pct,
            "stop_price": stop_price,
            "high_water_premium": high_water_premium,
            "target_pct": target_pct,
            "trailing_enabled": trailing_enabled,
            "trailing_activation_pct": trailing_activation_pct,
            "trailing_offset_pct": trailing_offset_pct,
            "trailing_lock_breakeven": trailing_lock_breakeven,
            "trailing_active": trailing_active,
            "orb_trail_activation_mfe": orb_trail_activation_mfe,
            "orb_trail_offset_pct": orb_trail_offset_pct,
            "orb_trail_min_lock_pct": orb_trail_min_lock_pct,
            "orb_trail_priority_over_regime": orb_trail_priority_over_regime,
            "orb_trail_regime_filter": orb_trail_regime_filter,
            "orb_trail_active": orb_trail_active,
            "orb_trail_stop_price": orb_trail_stop_price,
            "oi_trail_activation_mfe": oi_trail_activation_mfe,
            "oi_trail_offset_pct": oi_trail_offset_pct,
            "oi_trail_min_lock_pct": oi_trail_min_lock_pct,
            "oi_trail_priority_over_regime": oi_trail_priority_over_regime,
            "oi_trail_regime_filter": oi_trail_regime_filter,
            "oi_trail_active": oi_trail_active,
            "oi_trail_stop_price": oi_trail_stop_price,
            "exit_reason": exit_signal.exit_reason.value if exit_signal.exit_reason else None,
            "reason": exit_signal.reason,
            "run_id": self._run_id,
            **self._position_contract_fields(exit_signal),
        })
        append_jsonl(self._positions_path, record, logger=logger)
        self._publish(
            strategy_position_topic(),
            build_strategy_position_event(
                position=record,
                source="strategy_app",
                metadata=self._metadata(position_id=exit_signal.position_id, signal_id=exit_signal.signal_id),
            ),
        )

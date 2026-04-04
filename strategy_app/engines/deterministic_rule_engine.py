"""Phase-1 deterministic rule engine with regime-based routing."""

from __future__ import annotations

import logging
import math
import os
import time as wall_time
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from ..contracts import (
    Direction,
    ExitReason,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyEngine,
    StrategyVote,
    TradeSignal,
)
from ..logging.signal_logger import SignalLogger
from ..logging.decision_trace import (
    DecisionTraceBuilder,
    compact_metrics,
    position_state_payload,
    regime_context_payload,
    risk_state_payload,
    warmup_context_payload,
)
from ..position.tracker import PositionTracker
from ..risk.config import PositionRiskConfig
from ..risk.manager import RiskManager
from .decision_annotation import (
    annotate_signal_contract as apply_signal_contract,
    annotate_vote_contract as apply_vote_contract,
    derive_decision_mode,
    derive_reason_code,
)
from .entry_policy import EntryPolicy, EntryPolicyDecision, LongOptionEntryPolicy, PolicyConfig
from .regime import RegimeClassifier, RegimeSignal
from .snapshot_accessor import SnapshotAccessor
from .strategy_router import StrategyRouter

logger = logging.getLogger(__name__)

MIN_ENTRY_CONFIDENCE = 0.65
EXIT_CONFIDENCE = 0.65
DEFAULT_STRATEGY_PROFILE_ID = "det_core_v2"


class DeterministicRuleEngine(StrategyEngine):
    """Runs the regime layer, routes strategies, and emits one action."""

    def __init__(
        self,
        *,
        model_path: Optional[str] = None,
        min_confidence: float = MIN_ENTRY_CONFIDENCE,
        signal_logger: Optional[SignalLogger] = None,
        router: Optional[StrategyRouter] = None,
        default_risk_config: Optional[PositionRiskConfig] = None,
        entry_policy: Optional[EntryPolicy] = None,
        policy_config: Optional[PolicyConfig] = None,
        engine_mode: str = "deterministic",
        strategy_family_version: Optional[str] = None,
        strategy_profile_id: str = DEFAULT_STRATEGY_PROFILE_ID,
    ) -> None:
        self._regime = RegimeClassifier(model_path=model_path)
        self._router = router or StrategyRouter()
        self._tracker = PositionTracker()
        self._risk = RiskManager()
        self._log = signal_logger or SignalLogger()
        self._min_confidence = float(min_confidence)
        self._default_risk_config = default_risk_config or PositionRiskConfig()
        self._run_risk_config = self._default_risk_config
        self._default_policy_config = policy_config or PolicyConfig()
        self._injected_entry_policy = entry_policy
        self._entry_policy: EntryPolicy = entry_policy or LongOptionEntryPolicy(config=self._default_policy_config)
        self._post_halt_resume_boost_enabled = bool(self._default_policy_config.enable_post_halt_resume_boost)
        self._post_halt_resume_boost_score = float(self._default_policy_config.post_halt_resume_boost_score)
        self._startup_warmup_minutes = max(0.0, float(os.getenv("STRATEGY_STARTUP_WARMUP_MINUTES", "0") or 0.0))
        self._startup_warmup_events = max(0, int(os.getenv("STRATEGY_STARTUP_WARMUP_EVENTS", "0") or 0))
        self._strike_policy = str(os.getenv("STRATEGY_STRIKE_SELECTION_POLICY", "atm") or "atm").strip().lower()
        self._strike_max_otm_steps = max(0, int(os.getenv("STRATEGY_STRIKE_MAX_OTM_STEPS", "2") or 2))
        self._strike_min_oi = max(0.0, float(os.getenv("STRATEGY_STRIKE_MIN_OI", "50000") or 50000.0))
        self._strike_min_volume = max(0.0, float(os.getenv("STRATEGY_STRIKE_MIN_VOLUME", "15000") or 15000.0))
        self._strike_liquidity_weight = max(0.0, float(os.getenv("STRATEGY_STRIKE_LIQUIDITY_WEIGHT", "1.0") or 1.0))
        self._strike_affordability_weight = max(0.0, float(os.getenv("STRATEGY_STRIKE_AFFORDABILITY_WEIGHT", "0.25") or 0.25))
        self._strike_distance_penalty = max(0.0, float(os.getenv("STRATEGY_STRIKE_DISTANCE_PENALTY", "0.05") or 0.05))
        self._session_start_monotonic: Optional[float] = None
        self._session_event_count = 0
        self._current_session: Optional[date] = None
        self._regime_shift_streak: dict[str, int] = {}
        self._ml_score_all_snapshots = False
        self._engine_mode = "deterministic"
        self._strategy_family_version = str(strategy_family_version or "DET_V1").strip() or "DET_V1"
        self._strategy_profile_id = str(strategy_profile_id or DEFAULT_STRATEGY_PROFILE_ID).strip() or DEFAULT_STRATEGY_PROFILE_ID
        self._run_id: Optional[str] = None
        self._set_logger_context(None)
        logger.info("deterministic engine initialized min_confidence=%.2f", self._min_confidence)

    def set_run_context(self, run_id: Optional[str], metadata: Optional[dict[str, Any]] = None) -> None:
        self._run_id = str(run_id or "").strip() or None
        risk_payload = metadata.get("risk_config") if isinstance(metadata, dict) else None
        policy_payload = metadata.get("policy_config") if isinstance(metadata, dict) else None
        regime_payload = metadata.get("regime_config") if isinstance(metadata, dict) else None
        router_payload = metadata.get("router_config") if isinstance(metadata, dict) else None
        profile_override = None
        if isinstance(metadata, dict):
            profile_override = str(metadata.get("strategy_profile_id") or "").strip() or None
        router_has_strategy_override = bool(
            isinstance(router_payload, dict)
            and any(
                key in router_payload
                for key in ("enabled_entry_strategies", "regime_entry_map", "exit_strategies")
            )
        )
        if router_has_strategy_override and not profile_override:
            raise ValueError("strategy_profile_id is required for non-default deterministic strategy sets")
        self._run_risk_config = (
            PositionRiskConfig.from_payload(risk_payload) if isinstance(risk_payload, dict) else self._default_risk_config
        )
        if isinstance(policy_payload, dict):
            policy_cfg = PolicyConfig.from_payload(policy_payload)
            self._entry_policy = LongOptionEntryPolicy(config=policy_cfg)
            self._post_halt_resume_boost_enabled = bool(policy_cfg.enable_post_halt_resume_boost)
            self._post_halt_resume_boost_score = float(policy_cfg.post_halt_resume_boost_score)
        elif self._injected_entry_policy is not None:
            self._entry_policy = self._injected_entry_policy
            self._post_halt_resume_boost_enabled = bool(self._default_policy_config.enable_post_halt_resume_boost)
            self._post_halt_resume_boost_score = float(self._default_policy_config.post_halt_resume_boost_score)
        else:
            self._entry_policy = LongOptionEntryPolicy(config=self._default_policy_config)
            self._post_halt_resume_boost_enabled = bool(self._default_policy_config.enable_post_halt_resume_boost)
            self._post_halt_resume_boost_score = float(self._default_policy_config.post_halt_resume_boost_score)
        if isinstance(regime_payload, dict):
            self._regime.configure(regime_payload)
        if isinstance(router_payload, dict):
            self._router.configure(router_payload)
        if profile_override:
            self._strategy_profile_id = profile_override
        elif isinstance(router_payload, dict):
            self._strategy_profile_id = self._router.strategy_profile_id
        self._ml_score_all_snapshots = (
            _as_bool(metadata.get("ml_score_all_snapshots")) if isinstance(metadata, dict) else False
        )
        self._set_logger_context(run_id)

    def _set_logger_context(self, run_id: Optional[str]) -> None:
        payload = {
            "engine_mode": self._engine_mode,
            "strategy_family_version": self._strategy_family_version,
            "strategy_profile_id": self._strategy_profile_id,
        }
        try:
            self._log.set_run_context(run_id, payload)  # type: ignore[misc]
        except TypeError:
            self._log.set_run_context(run_id)

    def _decision_mode_from_policy(self, policy_decision: Optional[EntryPolicyDecision]) -> str:
        return derive_decision_mode(policy_decision)

    def _reason_code_from_policy(self, policy_decision: Optional[EntryPolicyDecision]) -> str:
        return derive_reason_code(policy_decision)

    def _annotate_vote_contract(self, vote: StrategyVote) -> None:
        apply_vote_contract(
            vote,
            engine_mode=self._engine_mode,
            strategy_family_version=self._strategy_family_version,
            strategy_profile_id=self._strategy_profile_id,
        )

    def _annotate_signal_contract(
        self,
        signal: TradeSignal,
        *,
        decision_mode: Optional[str] = None,
        decision_reason_code: Optional[str] = None,
        decision_metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        apply_signal_contract(
            signal,
            engine_mode=self._engine_mode,
            strategy_family_version=self._strategy_family_version,
            strategy_profile_id=self._strategy_profile_id,
            decision_mode=decision_mode,
            decision_reason_code=decision_reason_code,
            decision_metrics=decision_metrics,
        )

    def on_session_start(self, trade_date: date) -> None:
        self._current_session = trade_date
        self._session_start_monotonic = wall_time.monotonic()
        self._session_event_count = 0
        self._regime_shift_streak.clear()
        self._tracker.on_session_start(trade_date)
        self._risk.on_session_start(trade_date)
        for strategy in self._router.all_unique_strategies():
            strategy.on_session_start(trade_date)
        logger.info("deterministic engine session started: %s", trade_date.isoformat())

    def on_session_end(self, trade_date: date) -> None:
        if self._tracker.has_position:
            position = self._tracker.current_position
            exit_signal = self._tracker.force_exit(_session_end_snapshot(trade_date), ExitReason.TIME_STOP)
            if exit_signal is not None and position is not None:
                self._annotate_signal_contract(
                    exit_signal,
                    decision_mode="rule_vote",
                    decision_reason_code="time_stop",
                )
                self._log.log_signal(exit_signal, acted_on=True)
                self._handle_position_closed(exit_signal, position)
        stats = self._tracker.session_stats()
        logger.info("deterministic engine session ended: %s stats=%s", trade_date.isoformat(), stats)
        self._risk.on_session_end(trade_date)
        for strategy in self._router.all_unique_strategies():
            strategy.on_session_end(trade_date)
        self._current_session = None
        self._session_start_monotonic = None
        self._session_event_count = 0

    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        self._session_event_count += 1
        snap = SnapshotAccessor(snapshot)
        position = self._tracker.current_position
        risk = self._risk.context
        trace_blocker: Optional[str] = None
        warmup_blocked = False
        warmup_reason = ""

        self._risk.update(snap, position)

        if position is not None:
            system_exit = self._tracker.update(snap, risk)
            if system_exit is not None:
                self._annotate_signal_contract(system_exit, decision_mode="rule_vote")
                self._log.log_signal(system_exit, acted_on=True)
                self._handle_position_closed(system_exit, position)
                self._log.log_decision_trace(
                    self._build_position_trace(
                        snap=snap,
                        position=position,
                        votes=[],
                        signal=system_exit,
                        final_outcome="exit_taken",
                    )
                )
                return system_exit
            refreshed_position = self._tracker.current_position
            if refreshed_position is not None:
                self._log.log_position_manage(
                    position=refreshed_position,
                    timestamp=snap.timestamp_or_now,
                    snapshot_id=snap.snapshot_id,
                )

        regime_signal = self._regime.classify(snap)
        logger.debug(
            "snapshot=%s regime=%s conf=%.2f phase=%s reason=%s",
            snap.snapshot_id,
            regime_signal.regime.value,
            regime_signal.confidence,
            snap.session_phase,
            regime_signal.reason,
        )
        shadow_vote = self._build_ml_shadow_vote(snap=snap, regime_signal=regime_signal)

        strategies = self._router.get_strategies(regime_signal.regime, position)
        votes: list[StrategyVote] = []
        for strategy in strategies:
            try:
                vote = strategy.evaluate(snapshot, position, risk)
            except Exception:
                logger.exception("strategy failed strategy=%s", strategy.name)
                continue
            if vote is None:
                continue
            vote.raw_signals["_regime"] = regime_signal.regime.value
            vote.raw_signals["_regime_conf"] = round(regime_signal.confidence, 3)
            vote.raw_signals["_regime_reason"] = regime_signal.reason
            self._annotate_vote_contract(vote)
            votes.append(vote)

        if not votes:
            if shadow_vote is not None:
                self._annotate_vote_contract(shadow_vote)
                self._log.log_vote(shadow_vote)
            return None

        signal: Optional[TradeSignal] = None
        if position is not None:
            signal = self._process_exit_votes(votes, snap, position)

        if signal is None and position is None and not self._risk.is_halted and self._router.regime_allows_entry(regime_signal.regime):
            warmup_blocked, warmup_reason = self._entry_warmup_status()
            if warmup_blocked:
                trace_blocker = "warmup"
                for vote in votes:
                    if vote.signal_type == SignalType.ENTRY and vote.direction in (Direction.CE, Direction.PE):
                        vote.raw_signals["_entry_warmup_blocked"] = True
                        vote.raw_signals["_entry_warmup_reason"] = warmup_reason
                        self._annotate_vote_contract(vote)
            else:
                signal = self._process_entry_votes(votes, snap, risk, regime_signal)
                if signal is None:
                    trace_blocker = self._derive_entry_blocker(votes=votes, snap=snap, regime_signal=regime_signal)
        elif position is None:
            if self._risk.is_halted:
                trace_blocker = "risk_halt"
            elif self._router.regime_allows_entry(regime_signal.regime) is False:
                trace_blocker = "router_regime_block"

        for vote in votes:
            self._log.log_vote(vote)
        if shadow_vote is not None:
            self._annotate_vote_contract(shadow_vote)
            self._log.log_vote(shadow_vote)

        if position is not None:
            active_position = self._tracker.current_position or position
            self._log.log_decision_trace(
                self._build_position_trace(
                    snap=snap,
                    position=active_position,
                    votes=votes,
                    signal=signal,
                    final_outcome=("exit_taken" if signal is not None else "manage_only"),
                )
            )
        else:
            self._log.log_decision_trace(
                self._build_entry_trace(
                    snap=snap,
                    regime_signal=regime_signal,
                    votes=votes,
                    signal=signal,
                    blocker=trace_blocker,
                    warmup_blocked=warmup_blocked,
                    warmup_reason=warmup_reason,
                )
            )

        return signal

    def _process_exit_votes(
        self,
        votes: list[StrategyVote],
        snap: SnapshotAccessor,
        position: PositionContext,
    ) -> Optional[TradeSignal]:
        all_exit_votes = [
            vote
            for vote in votes
            if vote.signal_type == SignalType.EXIT
            and vote.direction == Direction.EXIT
            and vote.confidence >= EXIT_CONFIDENCE
        ]
        exit_votes = list(all_exit_votes)
        owned_exit_votes = [vote for vote in all_exit_votes if str(vote.strategy_name or "").strip().upper() == str(position.entry_strategy or "").strip().upper()]
        used_owned_pool = False
        if owned_exit_votes:
            exit_votes = owned_exit_votes
            used_owned_pool = True
            logger.debug(
                "using owned strategy exit-only pool entry_strategy=%s votes=%d",
                position.entry_strategy,
                len(owned_exit_votes),
            )
            best_vote = self._select_exit_vote(exit_votes, position)
            if best_vote is None:
                exit_votes = [
                    vote
                    for vote in votes
                    if vote.signal_type == SignalType.EXIT
                    and vote.direction == Direction.EXIT
                    and vote.confidence >= EXIT_CONFIDENCE
                ]
                logger.debug(
                    "owned exit not triggered, falling back to universal exit pool entry_strategy=%s",
                    position.entry_strategy,
                )
        else:
            logger.debug(
                "owned strategy exit votes missing, using universal exit pool entry_strategy=%s pool=%d",
                position.entry_strategy,
                len(exit_votes),
            )
        best_vote = self._select_exit_vote(exit_votes, position)
        if best_vote is None:
            return None
        if best_vote is not None and self._should_defer_regime_shift_exit(position, best_vote):
            non_regime_votes = [
                vote
                for vote in (all_exit_votes if used_owned_pool else exit_votes)
                if (vote.exit_reason or ExitReason.STRATEGY_EXIT) != ExitReason.REGIME_SHIFT
            ]
            best_vote = self._select_exit_vote(non_regime_votes, position)
            if best_vote is None:
                return None
        vote_reason = best_vote.exit_reason or ExitReason.STRATEGY_EXIT
        if vote_reason == ExitReason.REGIME_SHIFT:
            if not self._accept_regime_shift_exit(position):
                return None
        else:
            self._reset_regime_shift_streak(position.position_id)
        exit_signal = self._tracker.force_exit(snap, vote_reason)
        if exit_signal is None:
            return None
        exit_signal.votes = [best_vote]
        exit_signal.reason = best_vote.reason
        self._annotate_signal_contract(
            exit_signal,
            decision_mode=(best_vote.decision_mode or "rule_vote"),
            decision_reason_code=(str(vote_reason.value).strip().lower() if vote_reason else None),
            decision_metrics={
                "confidence": float(best_vote.confidence),
            },
        )
        self._log.log_signal(exit_signal, acted_on=True)
        self._handle_position_closed(exit_signal, position)
        return exit_signal

    def _process_entry_votes(
        self,
        votes: list[StrategyVote],
        snap: SnapshotAccessor,
        risk: RiskContext,
        regime_signal: RegimeSignal,
    ) -> Optional[TradeSignal]:
        avoid_votes = [vote for vote in votes if vote.direction == Direction.AVOID]
        if avoid_votes:
            best_avoid = max(avoid_votes, key=lambda item: item.confidence)
            logger.debug("entry vetoed strategy=%s reason=%s", best_avoid.strategy_name, best_avoid.reason)
            return None

        entry_votes = [
            vote
            for vote in votes
            if vote.signal_type == SignalType.ENTRY and vote.direction in (Direction.CE, Direction.PE)
        ]
        if not entry_votes:
            return None

        ce_votes = [vote for vote in entry_votes if vote.direction == Direction.CE]
        pe_votes = [vote for vote in entry_votes if vote.direction == Direction.PE]
        has_direction_conflict = bool(ce_votes and pe_votes)
        ml_can_resolve_direction_conflict = has_direction_conflict and self._entry_policy_can_resolve_direction_conflict()
        if has_direction_conflict and not ml_can_resolve_direction_conflict:
            logger.debug("entry blocked by direction conflict ce=%d pe=%d", len(ce_votes), len(pe_votes))
            return None

        if regime_signal.confidence < 0.60:
            logger.debug("entry blocked by low regime confidence=%.2f", regime_signal.confidence)
            return None

        if not snap.is_valid_entry_phase or self._risk.is_paused:
            return None
        ranked_entry_votes = sorted(entry_votes, key=lambda item: item.confidence, reverse=True)
        if ml_can_resolve_direction_conflict:
            scored_candidates: list[tuple[StrategyVote, EntryPolicyDecision]] = []
            for candidate in ranked_entry_votes:
                self._apply_strike_selection(candidate, snap)
                policy_decision = self._entry_policy.evaluate(snap, candidate, regime_signal, risk)
                self._annotate_policy(candidate, policy_decision)
                self._annotate_vote_contract(candidate)
                scored_candidates.append((candidate, policy_decision))
            eligible = [
                (candidate, policy_decision)
                for candidate, policy_decision in scored_candidates
                if candidate.confidence >= self._min_confidence and policy_decision.allowed
            ]
            if not eligible:
                logger.debug(
                    "entry blocked by ml direction resolution ce=%d pe=%d",
                    len(ce_votes),
                    len(pe_votes),
                )
                return None
            best_vote, best_decision = max(
                eligible,
                key=lambda item: (float(item[1].score), float(item[0].confidence)),
            )
            logger.debug(
                "entry direction resolved by ml dir=%s score=%.3f vote_conf=%.3f",
                best_vote.direction.value,
                float(best_decision.score),
                float(best_vote.confidence),
            )
            return self._build_entry_signal(best_vote, snap, risk, entry_votes, regime_signal, best_decision)

        for candidate in ranked_entry_votes:
            self._apply_strike_selection(candidate, snap)
            policy_decision = self._entry_policy.evaluate(snap, candidate, regime_signal, risk)
            self._annotate_policy(candidate, policy_decision)
            self._annotate_vote_contract(candidate)
            if candidate.confidence < self._min_confidence:
                continue
            if not policy_decision.allowed:
                continue
            return self._build_entry_signal(candidate, snap, risk, entry_votes, regime_signal, policy_decision)
        return None

    def _entry_policy_can_resolve_direction_conflict(self) -> bool:
        resolver = getattr(self._entry_policy, "can_resolve_direction_conflict", None)
        if not callable(resolver):
            return False
        try:
            return bool(resolver())
        except Exception:
            logger.exception("entry policy direction conflict resolver failed")
            return False

    def _build_entry_signal(
        self,
        best_vote: StrategyVote,
        snap: SnapshotAccessor,
        risk: RiskContext,
        all_votes: list[StrategyVote],
        regime_signal: RegimeSignal,
        policy_decision: Optional[EntryPolicyDecision] = None,
    ) -> Optional[TradeSignal]:
        direction = best_vote.direction
        if direction not in (Direction.CE, Direction.PE):
            return None

        selected_strike = best_vote.proposed_strike or snap.atm_strike
        if selected_strike is None or int(selected_strike) <= 0:
            return None
        selected_strike = int(selected_strike)

        premium = best_vote.proposed_entry_premium
        if premium is None or premium <= 0:
            premium = snap.option_ltp(direction.value, selected_strike)
        if premium is None or premium <= 0:
            return None

        expiry = None
        if snap.timestamp is not None and snap.days_to_expiry is not None:
            expiry = (snap.timestamp + timedelta(days=snap.days_to_expiry)).date()

        quality_score = policy_decision.score if policy_decision is not None else 1.0
        resume_boost_applied = False
        resume_boost = 0.0
        if self._post_halt_resume_boost_enabled and self._risk.post_halt_resume_boost_available:
            if self._risk.consume_post_halt_resume_boost():
                resume_boost_applied = True
                resume_boost = max(0.0, float(self._post_halt_resume_boost_score))
                quality_score = min(1.0, float(quality_score + resume_boost))
        combined_confidence = round(best_vote.confidence * regime_signal.confidence * quality_score, 3)
        if resume_boost_applied:
            best_vote.raw_signals["_post_halt_resume_boost_applied"] = True
            best_vote.raw_signals["_post_halt_resume_boost_score"] = round(resume_boost, 3)
            best_vote.raw_signals["_policy_score_after_resume_boost"] = round(quality_score, 3)
        stop_loss_pct, target_pct, trailing_cfg = self._resolve_entry_risk(best_vote, policy_decision)
        signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            timestamp=snap.timestamp_or_now,
            snapshot_id=snap.snapshot_id,
            signal_type=SignalType.ENTRY,
            direction=direction.value,
            strike=selected_strike,
            expiry=expiry,
            entry_premium=premium,
            stop_loss_pct=stop_loss_pct,
            target_pct=target_pct,
            trailing_enabled=trailing_cfg.trailing_enabled,
            trailing_activation_pct=trailing_cfg.trailing_activation_pct,
            trailing_offset_pct=trailing_cfg.trailing_offset_pct,
            trailing_lock_breakeven=trailing_cfg.trailing_lock_breakeven,
            orb_trail_activation_mfe=trailing_cfg.orb_trail.activation_mfe,
            orb_trail_offset_pct=trailing_cfg.orb_trail.trail_offset,
            orb_trail_min_lock_pct=trailing_cfg.orb_trail.min_lock_pct,
            orb_trail_priority_over_regime=trailing_cfg.orb_trail.priority_over_regime,
            orb_trail_regime_filter=trailing_cfg.orb_trail.regime_filter,
            oi_trail_activation_mfe=trailing_cfg.oi_trail.activation_mfe,
            oi_trail_offset_pct=trailing_cfg.oi_trail.trail_offset,
            oi_trail_min_lock_pct=trailing_cfg.oi_trail.min_lock_pct,
            oi_trail_priority_over_regime=trailing_cfg.oi_trail.priority_over_regime,
            oi_trail_regime_filter=trailing_cfg.oi_trail.regime_filter,
            max_lots=self._risk.compute_lots(
                entry_premium=premium,
                stop_loss_pct=stop_loss_pct,
                confidence=combined_confidence,
            ),
            entry_strategy_name=best_vote.strategy_name,
            entry_regime_name=regime_signal.regime.value,
            source="RULE",
            confidence=combined_confidence,
            reason=(
                f"[{regime_signal.regime.value}] {best_vote.strategy_name}: {best_vote.reason}"
                + (f" | resume_boost=+{resume_boost:.2f}" if resume_boost_applied else "")
            ),
            votes=all_votes,
        )
        entry_decision_mode = self._decision_mode_from_policy(policy_decision)
        self._annotate_signal_contract(
            signal,
            decision_mode=entry_decision_mode,
            decision_reason_code=self._reason_code_from_policy(policy_decision),
            decision_metrics={
                "confidence": float(combined_confidence),
                "policy_score": (float(policy_decision.score) if policy_decision is not None else None),
            },
        )

        opened = self._tracker.open_position(signal, snap)
        self._log.log_signal(signal, acted_on=True)
        self._log.log_position_open(signal, opened)
        logger.info(
            "entry signal regime=%s strategy=%s dir=%s strike=%s premium=%.2f conf=%.3f lots=%d session_pnl=%.2f%%",
            regime_signal.regime.value,
            best_vote.strategy_name,
            signal.direction,
            signal.strike,
            premium,
            combined_confidence,
            signal.max_lots,
            risk.session_pnl_total * 100.0,
        )
        return signal

    def _handle_position_closed(self, exit_signal: TradeSignal, position: PositionContext) -> None:
        self._reset_regime_shift_streak(position.position_id)
        self._risk.record_trade_result(
            pnl_pct=position.pnl_pct,
            lots=position.lots,
            entry_premium=position.entry_premium,
        )
        self._log.log_position_close(
            exit_signal=exit_signal,
            position=position,
            entry_premium=position.entry_premium,
            exit_premium=position.current_premium,
            pnl_pct=position.pnl_pct,
            mfe_pct=position.mfe_pct,
            mae_pct=position.mae_pct,
            bars_held=position.bars_held,
            stop_loss_pct=position.stop_loss_pct,
            stop_price=position.stop_price,
            high_water_premium=position.high_water_premium,
            target_pct=position.target_pct,
            trailing_enabled=position.trailing_enabled,
            trailing_activation_pct=position.trailing_activation_pct,
            trailing_offset_pct=position.trailing_offset_pct,
            trailing_lock_breakeven=position.trailing_lock_breakeven,
            trailing_active=position.trailing_active,
            orb_trail_activation_mfe=position.orb_trail_activation_mfe,
            orb_trail_offset_pct=position.orb_trail_offset_pct,
            orb_trail_min_lock_pct=position.orb_trail_min_lock_pct,
            orb_trail_priority_over_regime=position.orb_trail_priority_over_regime,
            orb_trail_regime_filter=position.orb_trail_regime_filter,
            orb_trail_active=position.orb_trail_active,
            orb_trail_stop_price=position.orb_trail_stop_price,
            oi_trail_activation_mfe=position.oi_trail_activation_mfe,
            oi_trail_offset_pct=position.oi_trail_offset_pct,
            oi_trail_min_lock_pct=position.oi_trail_min_lock_pct,
            oi_trail_priority_over_regime=position.oi_trail_priority_over_regime,
            oi_trail_regime_filter=position.oi_trail_regime_filter,
            oi_trail_active=position.oi_trail_active,
            oi_trail_stop_price=position.oi_trail_stop_price,
        )

    def _resolve_entry_risk(
        self,
        vote: StrategyVote,
        policy_decision: Optional[EntryPolicyDecision] = None,
    ) -> tuple[float, float, PositionRiskConfig]:
        cfg = self._run_risk_config
        stop_loss_pct = cfg.stop_loss_pct if cfg.stop_loss_pct is not None else float(vote.proposed_stop_loss_pct)
        target_pct = cfg.target_pct if cfg.target_pct is not None else float(vote.proposed_target_pct)
        adjustments = policy_decision.adjustments if policy_decision is not None else {}
        if "stop_loss_pct" in adjustments and cfg.stop_loss_pct is None:
            stop_loss_pct = float(adjustments["stop_loss_pct"])
        if "target_pct" in adjustments and cfg.target_pct is None:
            target_pct = float(adjustments["target_pct"])
        return max(0.0, float(stop_loss_pct)), max(0.0, float(target_pct)), cfg

    def _annotate_policy(self, vote: StrategyVote, decision: EntryPolicyDecision) -> None:
        vote.raw_signals["_policy_allowed"] = decision.allowed
        vote.raw_signals["_policy_score"] = round(decision.score, 3)
        vote.raw_signals["_policy_reason"] = decision.reason
        vote.raw_signals["_policy_checks"] = dict(decision.checks)

    def _build_ml_shadow_vote(
        self,
        *,
        snap: SnapshotAccessor,
        regime_signal: RegimeSignal,
    ) -> Optional[StrategyVote]:
        if not self._ml_score_all_snapshots:
            return None
        evaluator = getattr(self._entry_policy, "evaluate_shadow", None)
        if not callable(evaluator):
            return None
        direction, basis = self._shadow_direction_from_snapshot(snap)
        strike = snap.atm_strike
        premium = snap.option_ltp(direction.value, strike) if strike is not None and int(strike) > 0 else None
        vote = StrategyVote(
            strategy_name="ML_SHADOW",
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.timestamp_or_now.date().isoformat(),
            signal_type=SignalType.SKIP,
            direction=direction,
            confidence=0.0,
            reason="ml_shadow: pending",
            raw_signals={
                "_regime": regime_signal.regime.value,
                "_regime_conf": round(regime_signal.confidence, 3),
                "_regime_reason": regime_signal.reason,
                "_ml_shadow": True,
                "_ml_shadow_mode": "score_all_snapshots",
                "_ml_shadow_direction_basis": basis,
            },
            proposed_strike=(int(strike) if strike is not None and int(strike) > 0 else None),
            proposed_entry_premium=(float(premium) if premium is not None and premium > 0 else None),
        )
        try:
            decision = evaluator(snap=snap, vote=vote, regime=regime_signal)
        except Exception:
            logger.exception("ml shadow scoring failed snapshot=%s", snap.snapshot_id)
            return None
        self._annotate_policy(vote, decision)
        vote.reason = decision.reason
        vote.confidence = round(max(0.0, min(1.0, float(decision.score))), 3)
        self._annotate_vote_contract(vote)
        return vote

    @staticmethod
    def _shadow_direction_from_snapshot(snap: SnapshotAccessor) -> tuple[Direction, str]:
        r5 = snap.fut_return_5m
        if r5 is not None and float(r5) != 0.0:
            return (Direction.CE, "fut_return_5m") if float(r5) > 0 else (Direction.PE, "fut_return_5m")
        r15 = snap.fut_return_15m
        if r15 is not None and float(r15) != 0.0:
            return (Direction.CE, "fut_return_15m") if float(r15) > 0 else (Direction.PE, "fut_return_15m")
        pvwap = snap.price_vs_vwap
        if pvwap is not None and float(pvwap) != 0.0:
            return (Direction.CE, "price_vs_vwap") if float(pvwap) > 0 else (Direction.PE, "price_vs_vwap")
        return Direction.CE, "default_ce"

    def _select_exit_vote(
        self,
        exit_votes: list[StrategyVote],
        position: PositionContext,
    ) -> Optional[StrategyVote]:
        if not exit_votes:
            self._reset_regime_shift_streak(position.position_id)
            return None
        ranked_votes = [
            (
                self._router.exit_vote_priority(
                    position=position,
                    candidate_strategy=vote.strategy_name,
                    confidence=vote.confidence,
                ),
                vote.confidence,
                vote,
            )
            for vote in exit_votes
        ]
        eligible = [item for item in ranked_votes if item[0] > 0]
        if not eligible:
            self._reset_regime_shift_streak(position.position_id)
            return None
        return max(eligible, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def _should_defer_regime_shift_exit(position: PositionContext, vote: StrategyVote) -> bool:
        reason = vote.exit_reason or ExitReason.STRATEGY_EXIT
        if reason != ExitReason.REGIME_SHIFT:
            return False
        strategy_name = str(position.entry_strategy or "").strip().upper()
        if bool(position.trailing_enabled) and bool(position.trailing_active):
            return True
        if strategy_name == "ORB" and bool(position.orb_trail_active) and bool(position.orb_trail_priority_over_regime):
            return True
        if strategy_name == "OI_BUILDUP" and bool(position.oi_trail_active) and bool(position.oi_trail_priority_over_regime):
            return True
        return False

    def _accept_regime_shift_exit(self, position: PositionContext) -> bool:
        cfg = self._run_risk_config
        hold_floor = cfg.regime_shift_min_profit_hold_pct
        if hold_floor is not None and float(position.pnl_pct) >= float(hold_floor):
            self._reset_regime_shift_streak(position.position_id)
            return False
        required = max(1, int(cfg.regime_shift_confirm_bars))
        streak = int(self._regime_shift_streak.get(position.position_id, 0)) + 1
        self._regime_shift_streak[position.position_id] = streak
        return streak >= required

    def _reset_regime_shift_streak(self, position_id: Optional[str]) -> None:
        pid = str(position_id or "").strip()
        if not pid:
            return
        self._regime_shift_streak.pop(pid, None)

    def _entry_warmup_status(self) -> tuple[bool, str]:
        reasons: list[str] = []
        if self._startup_warmup_events > 0 and self._session_event_count < self._startup_warmup_events:
            reasons.append(f"events<{self._startup_warmup_events}")
        if self._startup_warmup_minutes > 0 and self._session_start_monotonic is not None:
            elapsed_minutes = max(0.0, (wall_time.monotonic() - self._session_start_monotonic) / 60.0)
            if elapsed_minutes < self._startup_warmup_minutes:
                reasons.append(f"runtime_minutes<{self._startup_warmup_minutes:g}")
        blocked = bool(reasons)
        return blocked, ",".join(reasons) if blocked else ""

    def _derive_entry_blocker(
        self,
        *,
        votes: list[StrategyVote],
        snap: SnapshotAccessor,
        regime_signal: RegimeSignal,
    ) -> str:
        avoid_votes = [vote for vote in votes if vote.direction == Direction.AVOID]
        if avoid_votes:
            return "avoid_veto"
        entry_votes = [
            vote
            for vote in votes
            if vote.signal_type == SignalType.ENTRY and vote.direction in (Direction.CE, Direction.PE)
        ]
        if not entry_votes:
            return "no_entry_votes"
        ce_votes = [vote for vote in entry_votes if vote.direction == Direction.CE]
        pe_votes = [vote for vote in entry_votes if vote.direction == Direction.PE]
        if ce_votes and pe_votes and not self._entry_policy_can_resolve_direction_conflict():
            return "direction_conflict"
        if regime_signal.confidence < 0.60:
            return "regime_confidence"
        if not snap.is_valid_entry_phase:
            return "entry_phase"
        if self._risk.is_paused:
            return "risk_pause"
        if all(float(vote.confidence) < self._min_confidence for vote in entry_votes):
            return "confidence_gate"
        policy_evaluated = False
        for vote in entry_votes:
            raw_signals = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
            if "_policy_allowed" in raw_signals or "_policy_reason" in raw_signals:
                policy_evaluated = True
                if bool(raw_signals.get("_policy_allowed")):
                    return "candidate_ranking"
        if policy_evaluated:
            return "policy_gate"
        return "no_selection"

    def _entry_candidate_gate_rows(
        self,
        *,
        vote: StrategyVote,
        signal: Optional[TradeSignal],
        blocker: Optional[str],
        regime_signal: RegimeSignal,
        warmup_blocked: bool,
        warmup_reason: str,
    ) -> tuple[list[dict[str, Any]], str, Optional[str], bool]:
        gates: list[dict[str, Any]] = [
            {
                "gate_id": "regime_classification",
                "gate_group": "regime",
                "status": "pass",
                "reason_code": None,
                "message": regime_signal.reason,
                "metrics": {"regime_confidence": regime_signal.confidence},
            }
        ]
        raw_signals = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
        selected = bool(
            signal is not None
            and signal.entry_strategy_name == vote.strategy_name
            and str(signal.direction or "").strip().upper() == str(vote.direction.value if vote.direction else "").strip().upper()
        )
        if vote.direction == Direction.AVOID:
            gates.append(
                {
                    "gate_id": "avoid_veto",
                    "gate_group": "router",
                    "status": "blocked",
                    "reason_code": "avoid_regime",
                    "message": vote.reason,
                    "metrics": {"confidence": vote.confidence},
                }
            )
            return gates, "blocked", "avoid_veto", False
        if blocker == "risk_halt":
            gates.append(
                {
                    "gate_id": "risk_halt",
                    "gate_group": "risk",
                    "status": "blocked",
                    "reason_code": "risk_halt",
                    "message": "risk halt prevented entry",
                    "metrics": {},
                }
            )
            return gates, "blocked", "risk_halt", False
        if blocker == "router_regime_block":
            gates.append(
                {
                    "gate_id": "router_regime_block",
                    "gate_group": "router",
                    "status": "blocked",
                    "reason_code": "avoid_regime",
                    "message": "router disabled entries for current regime",
                    "metrics": {},
                }
            )
            return gates, "blocked", "router_regime_block", False
        if warmup_blocked:
            gates.append(
                {
                    "gate_id": "warmup",
                    "gate_group": "warmup",
                    "status": "blocked",
                    "reason_code": "entry_warmup_block",
                    "message": warmup_reason,
                    "metrics": {},
                }
            )
            return gates, "blocked", "warmup", False
        if blocker == "direction_conflict":
            gates.append(
                {
                    "gate_id": "direction_conflict",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "direction_conflict",
                    "message": "entry blocked by unresolved direction conflict",
                    "metrics": {},
                }
            )
            return gates, "blocked", "direction_conflict", False
        if blocker == "regime_confidence":
            gates.append(
                {
                    "gate_id": "regime_confidence",
                    "gate_group": "regime",
                    "status": "blocked",
                    "reason_code": "regime_low_confidence",
                    "message": "regime confidence below threshold",
                    "metrics": {"regime_confidence": regime_signal.confidence},
                }
            )
            return gates, "blocked", "regime_confidence", False
        if blocker == "entry_phase":
            gates.append(
                {
                    "gate_id": "entry_phase",
                    "gate_group": "timing",
                    "status": "blocked",
                    "reason_code": "timing_block",
                    "message": "snapshot is outside valid entry phase",
                    "metrics": {},
                }
            )
            return gates, "blocked", "entry_phase", False
        if blocker == "risk_pause":
            gates.append(
                {
                    "gate_id": "risk_pause",
                    "gate_group": "risk",
                    "status": "blocked",
                    "reason_code": "risk_pause",
                    "message": "risk pause prevented entry",
                    "metrics": {},
                }
            )
            return gates, "blocked", "risk_pause", False
        gates.append(
            {
                "gate_id": "confidence_gate",
                "gate_group": "policy",
                "status": ("pass" if float(vote.confidence) >= self._min_confidence else "blocked"),
                "reason_code": ("below_min_confidence" if float(vote.confidence) < self._min_confidence else None),
                "message": None,
                "metrics": {"confidence": vote.confidence},
            }
        )
        if float(vote.confidence) < self._min_confidence:
            return gates, "blocked", "confidence_gate", False
        policy_allowed = raw_signals.get("_policy_allowed")
        policy_reason = str(raw_signals.get("_policy_reason") or "").strip()
        gates.append(
            {
                "gate_id": "policy_checks",
                "gate_group": "policy",
                "status": ("pass" if policy_allowed is True else "blocked"),
                "reason_code": (vote.decision_reason_code if policy_allowed is not True else "policy_allowed"),
                "message": policy_reason or None,
                "metrics": {
                    "policy_score": raw_signals.get("_policy_score"),
                },
            }
        )
        if policy_allowed is not True:
            return gates, "blocked", "policy_checks", False
        gates.append(
            {
                "gate_id": "candidate_ranking",
                "gate_group": "selection",
                "status": ("pass" if selected else "skipped"),
                "reason_code": (None if selected else "policy_allowed"),
                "message": (None if selected else "candidate passed but another candidate ranked higher"),
                "metrics": {},
            }
        )
        if selected:
            gates.append(
                {
                    "gate_id": "execution",
                    "gate_group": "execution",
                    "status": "pass",
                    "reason_code": None,
                    "message": "entry signal emitted",
                    "metrics": {"max_lots": signal.max_lots if signal is not None else None},
                }
            )
            return gates, "passed", None, True
        return gates, "skipped", "candidate_ranking", False

    def _build_entry_trace(
        self,
        *,
        snap: SnapshotAccessor,
        regime_signal: RegimeSignal,
        votes: list[StrategyVote],
        signal: Optional[TradeSignal],
        blocker: Optional[str],
        warmup_blocked: bool,
        warmup_reason: str,
    ) -> dict[str, Any]:
        builder = DecisionTraceBuilder(
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            engine_mode=self._engine_mode,
            decision_mode="rule_vote",
            evaluation_type="entry",
            run_id=self._run_id,
        )
        builder.set_context(
            position_state=position_state_payload(None),
            risk_state=risk_state_payload(self._risk),
            regime_context=regime_context_payload(regime_signal),
            warmup_context=warmup_context_payload(
                blocked=warmup_blocked,
                reason=warmup_reason,
                state={
                    "events_seen": int(self._session_event_count),
                    "min_events": int(self._startup_warmup_events),
                    "min_minutes": float(self._startup_warmup_minutes),
                },
            ),
        )
        builder.add_flow_gate(
            "regime_classification",
            gate_group="regime",
            status="pass",
            message=regime_signal.reason,
            metrics={"regime_confidence": regime_signal.confidence},
        )
        sorted_votes = sorted(
            [vote for vote in votes if vote.signal_type == SignalType.ENTRY or vote.direction == Direction.AVOID],
            key=lambda item: float(item.confidence),
            reverse=True,
        )
        for index, vote in enumerate(sorted_votes, start=1):
            candidate = builder.add_candidate(
                strategy_name=vote.strategy_name,
                candidate_type="strategy_vote",
                direction=(vote.direction.value if vote.direction is not None else None),
                confidence=vote.confidence,
                rank=index,
                metrics=compact_metrics(vote.decision_metrics),
            )
            gates, terminal_status, terminal_gate_id, selected = self._entry_candidate_gate_rows(
                vote=vote,
                signal=signal,
                blocker=blocker,
                regime_signal=regime_signal,
                warmup_blocked=warmup_blocked,
                warmup_reason=warmup_reason,
            )
            for gate in gates:
                builder.add_candidate_gate(candidate, **gate)
            builder.finalize_candidate(
                candidate,
                terminal_status=terminal_status,
                terminal_gate_id=terminal_gate_id,
                terminal_reason_code=vote.decision_reason_code,
                selected=selected,
                extra_metrics=compact_metrics(vote.decision_metrics),
            )
        final_outcome = "entry_taken" if signal is not None else ("blocked" if blocker is not None or warmup_blocked else "hold")
        return builder.finalize(
            final_outcome=final_outcome,
            primary_blocker_gate=("warmup" if warmup_blocked else blocker),
            summary_metrics={
                "vote_count": len(votes),
                "entry_vote_count": len([vote for vote in votes if vote.signal_type == SignalType.ENTRY]),
            },
        )

    def _build_position_trace(
        self,
        *,
        snap: SnapshotAccessor,
        position: PositionContext,
        votes: list[StrategyVote],
        signal: Optional[TradeSignal],
        final_outcome: str,
    ) -> dict[str, Any]:
        builder = DecisionTraceBuilder(
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            engine_mode=self._engine_mode,
            decision_mode="rule_vote",
            evaluation_type=("exit" if signal is not None else "manage"),
            run_id=self._run_id,
        )
        regime_signal = self._regime.classify(snap)
        builder.set_context(
            position_state=position_state_payload(position),
            risk_state=risk_state_payload(self._risk),
            regime_context=regime_context_payload(regime_signal),
            warmup_context=warmup_context_payload(blocked=False, reason=None, state={}),
        )
        exit_votes = [
            vote
            for vote in votes
            if vote.signal_type == SignalType.EXIT and vote.direction == Direction.EXIT
        ]
        selected_strategy = None
        if signal is not None and isinstance(signal.votes, list) and signal.votes:
            selected_strategy = str(signal.votes[0].strategy_name or "").strip()
        for index, vote in enumerate(sorted(exit_votes, key=lambda item: float(item.confidence), reverse=True), start=1):
            candidate = builder.add_candidate(
                strategy_name=vote.strategy_name,
                candidate_type="exit_vote",
                direction=(vote.direction.value if vote.direction is not None else None),
                confidence=vote.confidence,
                rank=index,
                metrics={"confidence": vote.confidence},
            )
            builder.add_candidate_gate(
                candidate,
                "exit_confidence",
                gate_group="exit",
                status=("pass" if float(vote.confidence) >= EXIT_CONFIDENCE else "blocked"),
                reason_code=(None if float(vote.confidence) >= EXIT_CONFIDENCE else "below_min_confidence"),
                message=vote.reason,
                metrics={"confidence": vote.confidence},
            )
            if float(vote.confidence) < EXIT_CONFIDENCE:
                builder.finalize_candidate(
                    candidate,
                    terminal_status="blocked",
                    terminal_gate_id="exit_confidence",
                    terminal_reason_code=vote.exit_reason.value if vote.exit_reason is not None else vote.decision_reason_code,
                    selected=False,
                )
                continue
            if signal is not None and selected_strategy == vote.strategy_name:
                builder.add_candidate_gate(
                    candidate,
                    "exit_selection",
                    gate_group="selection",
                    status="pass",
                    reason_code=(vote.exit_reason.value if vote.exit_reason is not None else None),
                    message="exit signal emitted",
                    metrics={},
                )
                builder.finalize_candidate(
                    candidate,
                    terminal_status="passed",
                    terminal_gate_id=None,
                    terminal_reason_code=vote.exit_reason.value if vote.exit_reason is not None else vote.decision_reason_code,
                    selected=True,
                )
            else:
                builder.add_candidate_gate(
                    candidate,
                    "exit_selection",
                    gate_group="selection",
                    status="skipped",
                    reason_code=(vote.exit_reason.value if vote.exit_reason is not None else vote.decision_reason_code),
                    message="candidate evaluated but not selected",
                    metrics={},
                )
                builder.finalize_candidate(
                    candidate,
                    terminal_status="skipped",
                    terminal_gate_id="exit_selection",
                    terminal_reason_code=vote.exit_reason.value if vote.exit_reason is not None else vote.decision_reason_code,
                    selected=False,
                )
        primary_blocker = None if signal is not None else "no_exit_trigger"
        return builder.finalize(
            final_outcome=final_outcome,
            primary_blocker_gate=primary_blocker,
            summary_metrics={
                "exit_vote_count": len(exit_votes),
                "bars_held": position.bars_held,
                "pnl_pct": position.pnl_pct,
            },
        )

    def _apply_strike_selection(self, vote: StrategyVote, snap: SnapshotAccessor) -> None:
        if self._strike_policy != "oi_volume_ranked":
            return
        direction = vote.direction
        if direction not in (Direction.CE, Direction.PE):
            return
        atm = snap.atm_strike
        if atm is None or int(atm) <= 0:
            return
        atm = int(atm)

        strikes = snap.available_strikes()
        if not strikes:
            return

        step = snap.strike_step()
        if step is None or step <= 0:
            return

        side = direction.value
        candidate_strikes: list[int] = []
        for n in range(0, self._strike_max_otm_steps + 1):
            if side == "CE":
                strike = atm + (n * step)
            else:
                strike = atm - (n * step)
            if strike in strikes:
                candidate_strikes.append(strike)
        if not candidate_strikes:
            return

        atm_premium = snap.option_ltp(side, atm)
        best: Optional[tuple[float, int, float, float, float, int]] = None
        for strike in candidate_strikes:
            premium = snap.option_ltp(side, strike)
            oi = snap.option_oi(side, strike)
            volume = snap.option_volume(side, strike)
            if premium is None or premium <= 0:
                continue
            oi_value = float(oi) if oi is not None and oi > 0 else 0.0
            volume_value = float(volume) if volume is not None and volume > 0 else 0.0
            if strike != atm and (oi_value < self._strike_min_oi or volume_value < self._strike_min_volume):
                continue
            liquidity_score = (0.6 * math.log1p(oi_value)) + (0.4 * math.log1p(volume_value))
            affordability = 1.0
            if atm_premium is not None and atm_premium > 0:
                affordability = float(atm_premium / premium)
            distance_steps = abs(strike - atm) // step
            score = (
                (self._strike_liquidity_weight * liquidity_score)
                + (self._strike_affordability_weight * affordability)
                - (self._strike_distance_penalty * float(distance_steps))
            )
            row = (score, strike, float(premium), oi_value, volume_value, int(distance_steps))
            if best is None or row[0] > best[0]:
                best = row

        if best is None:
            return
        _, selected_strike, selected_premium, selected_oi, selected_volume, selected_distance = best
        vote.proposed_strike = int(selected_strike)
        vote.proposed_entry_premium = float(selected_premium)
        vote.raw_signals["_strike_policy"] = "oi_volume_ranked"
        vote.raw_signals["_strike_selected"] = int(selected_strike)
        vote.raw_signals["_strike_selected_premium"] = float(round(selected_premium, 4))
        vote.raw_signals["_strike_selected_oi"] = float(round(selected_oi, 2))
        vote.raw_signals["_strike_selected_volume"] = float(round(selected_volume, 2))
        vote.raw_signals["_strike_selected_distance_steps"] = int(selected_distance)


class _SessionEndSnapshot(SnapshotAccessor):
    """Minimal accessor used to force a session-end exit."""

    def __init__(self, trade_date: date) -> None:
        timestamp = datetime.combine(trade_date, time(hour=15, minute=15))
        super().__init__(
            {
                "snapshot_id": f"SESSION_END_{trade_date.isoformat()}",
                "session_context": {
                    "snapshot_id": f"SESSION_END_{trade_date.isoformat()}",
                    "timestamp": timestamp.isoformat(),
                    "date": trade_date.isoformat(),
                    "session_phase": "PRE_CLOSE",
                },
            }
        )


def _session_end_snapshot(trade_date: date) -> _SessionEndSnapshot:
    return _SessionEndSnapshot(trade_date)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed:
        return None
    return float(parsed)

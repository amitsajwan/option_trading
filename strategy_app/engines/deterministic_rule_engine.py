"""Phase-1 deterministic rule engine with regime-based routing."""

from __future__ import annotations

import json
import logging
import math
import os
import time as wall_time
import uuid
from collections import deque
from datetime import date, datetime, time, timedelta
from pathlib import Path
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
from contracts_app import isoformat_ist
from ..logging.signal_logger import SignalLogger
from ..logging.decision_trace import (
    DecisionTraceBuilder,
    compact_metrics,
    position_state_payload,
    regime_context_payload,
    risk_state_payload,
    warmup_context_payload,
)
from ..utils.env import safe_float as _safe_float
from ..position.tracker import PositionTracker
from ..risk.config import PositionRiskConfig
from ..brain.playbook_brain import PLAYBOOK_EXIT_KEY
from ..risk.manager import RiskManager
from ..signals.decision_annotation import (
    annotate_signal_contract as apply_signal_contract,
    annotate_vote_contract as apply_vote_contract,
    derive_decision_mode,
    derive_reason_code,
)
from ..policy.entry_policy import EntryPolicy, EntryPolicyDecision, LongOptionEntryPolicy, PolicyConfig
from ..ml.direction_ml_policy import maybe_wrap_with_direction_ml
from .direction_consensus import extract_ml_direction_hint, resolve_direction_consensus
from .entry_gates import (
    compute_regime_tag,
    is_in_configured_time_window,
    is_session_regime_allowed,
)
from .entry_config import EntryConfig
from .eval_timing import PhaseTimer
from .entry_pipeline_contracts import EntryContext
from .entry_pipeline_gates import (
    build_entry_pipeline,
    evaluate_v2 as _evaluate_v2,
)
from ..market.depth_context import DepthContext
from ..runtime.eval_context import clear_depth_context, set_depth_context
from ..runtime.redis_depth_reader import RedisDepthReader
from .profiles import (
    PRODUCTION_DEFAULT_PROFILE_ID,
    PROFILE_DEBIT_MULTI_V1,
    PROFILE_R1S_TOP3_PAPER_V1,
    PROFILE_TRADER_MASTER_V1,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1,
    PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1,
    PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1,
    PROFILE_TRADER_MASTER_LIVE_V1,
    get_risk_config,
)
from ..brain.brain import BrainDecision, TradingBrain
from ..brain.context import DayContext
from ..runtime.runtime_artifacts import resolve_runtime_artifact_paths

_PROFILES_RELAX_REGIME_CONF = frozenset(
    {
        PROFILE_R1S_TOP3_PAPER_V1,
        PROFILE_DEBIT_MULTI_V1,
        PROFILE_TRADER_MASTER_V1,
        PROFILE_TRADER_MASTER_ML_ENTRY_V1,
        PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1,
        PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1,
        PROFILE_TRADER_MASTER_LIVE_V1,
    }
)

_PROFILES_ML_ENTRY_DET_DIRECTION = frozenset({
    PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1,
    PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1,
})

_PROFILES_ML_ENTRY_CONSENSUS = frozenset({
    PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1,
})
from ..market.regime import RegimeClassifier, RegimeSignal
from ..market.snapshot_accessor import SnapshotAccessor
from .strategy_router import StrategyRouter
from ..policy.velocity_entry_policy import VelocityEnhancedEntryPolicy
from ..policy.velocity_regime_classifier import VelocityEnhancedRegimeClassifier
from ..constants import EXIT_CONFIDENCE, MIN_ENTRY_CONFIDENCE, SOFT_CLOSE_MINUTE
from ..utils.env import as_bool

logger = logging.getLogger(__name__)


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
        strategy_profile_id: str = PRODUCTION_DEFAULT_PROFILE_ID,
        depth_reader: Optional[RedisDepthReader] = None,
    ) -> None:
        self._velocity_enhanced = as_bool(os.getenv("STRATEGY_ENHANCED_VELOCITY"))
        self._regime = (
            VelocityEnhancedRegimeClassifier(model_path=model_path)
            if self._velocity_enhanced
            else RegimeClassifier(model_path=model_path)
        )
        self._router = router or StrategyRouter()
        self._tracker = PositionTracker()
        self._risk = RiskManager()
        self._log = signal_logger or SignalLogger()
        # Live-only entry gate: when set, this engine only opens live-eligible
        # (GOOD) entries — used to run a parallel LIVE book whose single slot is
        # never blocked by low-grade paper trades. Default off (= take everything).
        self._entry_live_only_gate = as_bool(os.getenv("ENTRY_LIVE_ONLY_GATE", "false"))
        self._min_confidence = float(min_confidence)
        if default_risk_config is not None:
            self._default_risk_config = default_risk_config
        else:
            _profile_risk = get_risk_config(strategy_profile_id)
            self._default_risk_config = (
                PositionRiskConfig.from_payload(_profile_risk) if _profile_risk else PositionRiskConfig()
            )
        self._run_risk_config = self._default_risk_config
        self._default_policy_config = policy_config or PolicyConfig()
        self._injected_entry_policy = entry_policy
        self._entry_policy: EntryPolicy = entry_policy or self._build_entry_policy(self._default_policy_config)
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
        self._strategy_profile_id = str(strategy_profile_id or PRODUCTION_DEFAULT_PROFILE_ID).strip() or PRODUCTION_DEFAULT_PROFILE_ID
        self._run_id: Optional[str] = None
        self._brain: TradingBrain = TradingBrain.from_env()
        self._day_context: Optional[DayContext] = None
        self._brain_state_path: Optional[Path] = None
        # Rolling buffers for multi-bar trap signal detection in shadow scorer.
        self._iv_buf: deque = deque(maxlen=3)   # (ce_iv, pe_iv) per bar
        self._pvwap_buf: deque = deque(maxlen=2) # price_vs_vwap per bar
        # E8 session-once daily regime tag — computed lazily after ORB resolves.
        self._session_regime_tag: Optional[str] = None
        # Trader-discipline: track last exit for cooldown / direction-flip rules.
        self._last_stop_bar: Optional[int] = None        # event count when last STOP_LOSS fired
        self._last_exit_direction: Optional[str] = None  # "CE" | "PE" at last stop exit
        self._last_any_exit_bar: Optional[int] = None   # event count of ANY exit (for general spacing)
        self._last_zero_mfe_bar: Optional[int] = None   # event count when a zero-MFE exit occurred
        self._last_zero_mfe_direction: Optional[str] = None  # direction of that zero-MFE trade
        # Optional live depth feed (None in replay/offline — signals degrade gracefully).
        self._depth_reader: Optional[RedisDepthReader] = depth_reader
        # Per-evaluate depth snapshot — set at start of evaluate(), read by shadow scorer.
        self._current_depth_ctx: Optional[DepthContext] = None
        # Entry pipeline v2 — strangler flag (default off; set STRATEGY_ENTRY_PIPELINE_V2=1 to enable).
        self._entry_pipeline_v2: bool = as_bool(os.getenv("STRATEGY_ENTRY_PIPELINE_V2", "0"))
        # Most-recent v2 gate-cascade decision trace (one bar). Consumed by the sim
        # replay + Terminal decision view. None until the first v2 evaluation.
        self.last_entry_trace: Optional[dict[str, Any]] = None
        # Per-phase evaluate() profiling — off by default, enable in SIM/replay
        # with STRATEGY_EVAL_TIMING=1 to see where the per-bar budget goes.
        self._eval_timer = PhaseTimer(enabled=as_bool(os.getenv("STRATEGY_EVAL_TIMING", "0")))
        self._entry_config: EntryConfig = EntryConfig.from_env()
        try:
            self._entry_config.assert_consistency()
        except ValueError as _ecfg_err:
            logger.warning("entry_config assertion failed: %s", _ecfg_err)
        self._set_logger_context(None)
        logger.info(
            "deterministic engine initialized min_confidence=%.2f velocity_enhanced=%s brain_enabled=%s pipeline_v2=%s",
            self._min_confidence,
            self._velocity_enhanced,
            self._brain.enabled,
            self._entry_pipeline_v2,
        )

    def _build_entry_policy(self, config: PolicyConfig) -> EntryPolicy:
        if self._velocity_enhanced:
            base: EntryPolicy = VelocityEnhancedEntryPolicy(config=config)
        else:
            base = LongOptionEntryPolicy(config=config)
        return maybe_wrap_with_direction_ml(base)

    def set_run_context(self, run_id: Optional[str], metadata: Optional[dict[str, Any]] = None) -> None:
        # E6-S1: preserve session run_id set at startup — snapshot events carry no
        # run_id so calling set_run_context from the consumer would clear it.
        new_run_id = str(run_id or "").strip() or None
        if new_run_id:
            self._run_id = new_run_id
        # E6-S3: skip rebuilding entry policy (which reloads the ML model bundle)
        # when the call comes from a snapshot event with no config payload.
        # Only rebuild when metadata carries an actual policy/risk/regime override.
        _has_payload = isinstance(metadata, dict) and any(
            k in metadata for k in ("policy_config", "risk_config", "regime_config", "router_config")
        )
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
        # Only overwrite risk config when explicitly provided in metadata.
        # The orchestrator serialises the run command's risk_config for every snapshot
        # event. When the UI queues a run without an explicit override the stored doc
        # contains {"stop_loss_pct": null, "target_pct": null, …} — a non-empty dict
        # with all-null values. That dict is truthy, so a simple `if risk_payload:`
        # guard fires and resets the profile config to all-None defaults.
        # The correct check: skip when the payload has no non-null values, i.e.
        # there is genuinely no meaningful override being requested.
        _has_override = isinstance(risk_payload, dict) and any(
            v is not None for v in risk_payload.values()
        )
        if _has_override:
            self._run_risk_config = PositionRiskConfig.from_payload(risk_payload)
        if isinstance(policy_payload, dict):
            policy_cfg = PolicyConfig.from_payload(policy_payload)
            self._entry_policy = self._build_entry_policy(policy_cfg)
            self._post_halt_resume_boost_enabled = bool(policy_cfg.enable_post_halt_resume_boost)
            self._post_halt_resume_boost_score = float(policy_cfg.post_halt_resume_boost_score)
        elif self._injected_entry_policy is not None:
            self._entry_policy = self._injected_entry_policy
            self._post_halt_resume_boost_enabled = bool(self._default_policy_config.enable_post_halt_resume_boost)
            self._post_halt_resume_boost_score = float(self._default_policy_config.post_halt_resume_boost_score)
        elif _has_payload:
            # Only rebuild on actual overrides — not on per-snapshot heartbeat calls.
            self._entry_policy = self._build_entry_policy(self._default_policy_config)
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
            as_bool(metadata.get("ml_score_all_snapshots")) if isinstance(metadata, dict) else False
        )
        self._set_logger_context(new_run_id or self._run_id)

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
        self._iv_buf.clear()
        self._pvwap_buf.clear()
        self._session_regime_tag = None
        self._eval_timer.reset()
        self._last_stop_bar = None
        self._last_exit_direction = None
        self._last_any_exit_bar = None
        self._last_zero_mfe_bar = None
        self._last_zero_mfe_direction = None
        self._tracker.on_session_start(trade_date)
        # Brain morning briefing: loads daily features + cross-session carry.
        # Must run before risk manager so carry-based consecutive_losses can
        # be used to initialise the risk context if needed.
        self._day_context = self._brain.morning_briefing(trade_date)
        self._write_brain_state(trade_date)
        self._risk.on_session_start(trade_date)
        # Carry over consecutive losses from previous session into risk manager
        carry = self._day_context.session_carry
        if carry.consecutive_losses_at_close > 0:
            self._risk.context.consecutive_losses = carry.consecutive_losses_at_close
            if carry.consecutive_losses_at_close >= self._risk.context.max_consecutive_losses:
                self._risk.context.consecutive_loss_limit = True
                logger.warning(
                    "session started with carry consecutive_losses=%d from prior session — risk paused",
                    carry.consecutive_losses_at_close,
                )
        for strategy in self._router.all_unique_strategies():
            strategy.on_session_start(trade_date)
        logger.info(
            "deterministic engine session started: %s day_score=%s carry_losses=%d",
            trade_date.isoformat(),
            self._day_context.day_score.value,
            carry.consecutive_losses_at_close,
        )

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
        if self._eval_timer.enabled:
            logger.info("eval timing %s | %s", trade_date.isoformat(), self._eval_timer.format_summary())
        self._risk.on_session_end(trade_date)
        # Persist session summary for cross-session carry
        self._brain.save_session_summary(trade_date)
        for strategy in self._router.all_unique_strategies():
            strategy.on_session_end(trade_date)
        self._current_session = None
        self._session_start_monotonic = None
        self._session_event_count = 0
        self._day_context = None

    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        # Thin wrapper: count one bar and time the whole body so SIM profiling
        # has a `total` to compute per-phase percentages against. No-op overhead
        # when STRATEGY_EVAL_TIMING is off.
        self._eval_timer.mark_bar()
        with self._eval_timer.measure("total"):
            return self._evaluate_impl(snapshot)

    def _evaluate_impl(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        self._session_event_count += 1
        snap = SnapshotAccessor(snapshot)
        # Update rolling IV and VWAP-bias buffers before any shadow-scorer calls.
        self._iv_buf.append((snap.atm_ce_iv, snap.atm_pe_iv))
        self._pvwap_buf.append(snap.price_vs_vwap)
        # Read live depth side-channel once per bar (None in replay — graceful fallback).
        with self._eval_timer.measure("depth_read"):
            self._current_depth_ctx = (
                self._depth_reader.read_depth() if self._depth_reader is not None else None
            )
        set_depth_context(self._current_depth_ctx)
        position = self._tracker.current_position
        risk = self._risk.context
        trace_blocker: Optional[str] = None
        warmup_blocked = False
        warmup_reason = ""

        with self._eval_timer.measure("risk_update"):
            self._risk.update(snap, position)

        if position is not None:
            with self._eval_timer.measure("manage_position"):
                system_exit = self._manage_open_position(snap, position, risk)
            if system_exit is not None:
                return system_exit

        with self._eval_timer.measure("regime"):
            regime_signal = self._regime.classify(snap)
        logger.debug(
            "snapshot=%s regime=%s conf=%.2f phase=%s reason=%s",
            snap.snapshot_id,
            regime_signal.regime.value,
            regime_signal.confidence,
            snap.session_phase,
            regime_signal.reason,
        )
        with self._eval_timer.measure("shadow_vote"):
            shadow_vote = self._build_ml_shadow_vote(snap=snap, regime_signal=regime_signal)
        with self._eval_timer.measure("collect_votes"):
            votes = self._collect_votes(snapshot, snap, regime_signal, position, risk, shadow_vote)
        if not votes:
            self._emit_decision_summary(
                snap=snap,
                action=("manage_only" if position is not None else "hold"),
                regime_signal=regime_signal,
                votes=[],
                signal=None,
                position=position,
                blocking_gate=(None if position is not None else "no_strategy_votes"),
            )
            return None

        signal: Optional[TradeSignal] = None
        if position is not None:
            with self._eval_timer.measure("exit_votes"):
                signal = self._process_exit_votes(votes, snap, position)

        if signal is None and position is None and not self._risk.is_halted and self._router.regime_allows_entry(regime_signal.regime):
            _ts = snap.timestamp
            if _ts is not None and (_ts.hour * 60 + _ts.minute) >= SOFT_CLOSE_MINUTE:
                trace_blocker = "soft_close_no_entry"
            else:
                warmup_blocked, warmup_reason = self._entry_warmup_status()
                if warmup_blocked:
                    trace_blocker = "warmup"
                    for vote in votes:
                        if vote.signal_type == SignalType.ENTRY and vote.direction in (Direction.CE, Direction.PE):
                            vote.raw_signals["_entry_warmup_blocked"] = True
                            vote.raw_signals["_entry_warmup_reason"] = warmup_reason
                            self._annotate_vote_contract(vote)
                else:
                    with self._eval_timer.measure("entry_votes"):
                        signal = self._process_entry_votes(votes, snap, risk, regime_signal)
                    if signal is None:
                        trace_blocker = self._derive_entry_blocker(votes=votes, snap=snap, regime_signal=regime_signal)
        elif position is None:
            if self._risk.is_halted:
                trace_blocker = self._risk.halt_reason or "risk_halt"
            elif self._risk.is_paused:
                trace_blocker = self._risk.pause_reason or "risk_pause"
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
            self._emit_decision_summary(
                snap=snap,
                action=("exit_taken" if signal is not None else "manage_only"),
                regime_signal=regime_signal,
                votes=votes,
                signal=signal,
                position=active_position,
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
            self._emit_decision_summary(
                snap=snap,
                action=(
                    "entry_taken" if signal is not None
                    else ("blocked" if (trace_blocker is not None or warmup_blocked) else "hold")
                ),
                regime_signal=regime_signal,
                votes=votes,
                signal=signal,
                position=None,
                blocking_gate=("warmup" if warmup_blocked else trace_blocker),
                warmup_blocked=warmup_blocked,
            )

        return signal

    def _manage_open_position(
        self,
        snap: SnapshotAccessor,
        position: PositionContext,
        risk: RiskContext,
    ) -> Optional[TradeSignal]:
        """Check for system exits (stop, target, time) and log manage events."""
        # Populate current shadow score when needed by any dynamic exit logic.
        if (
            position.stagnant_exit_condition == "shadow_score_crossed_zero"
            or as_bool(os.getenv("DYNAMIC_SCRATCH_ENABLED", "false"))
            or as_bool(os.getenv("STAGNANT_PROFIT_EXIT_ENABLED", "false"))
        ):
            try:
                _, _, _shadow_score = self._shadow_direction_from_snapshot(snap)
                position.current_shadow_score = float(_shadow_score)
            except Exception:
                # Do not block manage loop if scorer fails; leave score as-is.
                pass
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
            self._emit_decision_summary(
                snap=snap,
                action="exit_taken",
                regime_signal=None,
                votes=[],
                signal=system_exit,
                position=position,
            )
            return system_exit
        refreshed_position = self._tracker.current_position
        if refreshed_position is not None:
            self._log.log_position_manage(
                position=refreshed_position,
                timestamp=snap.timestamp_or_now,
                snapshot_id=snap.snapshot_id,
            )
        return None

    def _collect_votes(
        self,
        snapshot: SnapshotPayload,
        snap: SnapshotAccessor,
        regime_signal: RegimeSignal,
        position: Optional[PositionContext],
        risk: RiskContext,
        shadow_vote: Optional[StrategyVote],
    ) -> list[StrategyVote]:
        """Route to active strategies and collect votes; log shadow vote if no real votes."""
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
            self._grade_and_tier_vote(vote, snap, regime_signal.regime.value, risk)
            self._annotate_vote_contract(vote)
            votes.append(vote)

        if not votes and shadow_vote is not None:
            self._annotate_vote_contract(shadow_vote)
            self._log.log_vote(shadow_vote)
        return votes

    def _grade_and_tier_vote(self, vote, snap, regime_name, risk) -> None:
        """Attach entry-quality grade + live/paper tier to an ENTRY vote.

        Observational only — NEVER blocks. Paper takes every trade; the tier
        labels which subset would fire on real money. Gated by
        ENTRY_TIERING_ENABLED (default on). Skipped for exit votes and for
        direction modes that don't produce composite direction scores.
        """
        from ..utils.env import as_bool
        if not as_bool(os.getenv("ENTRY_TIERING_ENABLED", "true")):
            return
        try:
            if getattr(vote, "signal_type", None) != SignalType.ENTRY:
                return
            raw = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
            from ..signals.entry_quality import grade_entry_from_raw, decide_tier
            quality = grade_entry_from_raw(
                raw, snap, direction=vote.direction, regime=regime_name,
            )
            if quality is None:
                return
            raw.update(quality.as_raw_signals())
            tier = decide_tier(
                quality.grade, risk, confidence=float(vote.confidence or 0.0),
            )
            raw.update(tier.as_raw_signals())
            vote.raw_signals = raw
        except Exception:
            logger.exception("entry tiering failed snapshot=%s", getattr(snap, "snapshot_id", "?"))

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
        owned_exit_votes = [
            vote for vote in all_exit_votes
            if str(vote.strategy_name or "").strip().upper() == str(position.entry_strategy or "").strip().upper()
        ]
        # Prefer owned-strategy pool; fall back to all exit votes if it yields no winner.
        if owned_exit_votes:
            logger.debug(
                "using owned strategy exit-only pool entry_strategy=%s votes=%d",
                position.entry_strategy,
                len(owned_exit_votes),
            )
            best_vote = self._select_exit_vote(owned_exit_votes, position)
            if best_vote is None:
                logger.debug(
                    "owned exit not triggered, falling back to universal exit pool entry_strategy=%s",
                    position.entry_strategy,
                )
                best_vote = self._select_exit_vote(all_exit_votes, position)
        else:
            logger.debug(
                "owned strategy exit votes missing, using universal exit pool entry_strategy=%s pool=%d",
                position.entry_strategy,
                len(all_exit_votes),
            )
            best_vote = self._select_exit_vote(all_exit_votes, position)

        if best_vote is None:
            return None
        if self._should_defer_regime_shift_exit(position, best_vote):
            non_regime_votes = [
                vote for vote in all_exit_votes
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
        # E8 gates: env-driven time-window + daily-regime filters. Both default
        # off; when configured they apply across all profiles.
        if not is_in_configured_time_window(snap):
            # Blocker captured centrally as "entry_time_windows" in the per-tick
            # decisions.jsonl summary (see _derive_entry_blocker).
            return None
        tagger = os.getenv("ENTRY_REGIME_TAGGER", "").strip()
        if tagger:
            if self._session_regime_tag is None:
                tag = compute_regime_tag(tagger, snap)
                if tag != "unknown":
                    self._session_regime_tag = tag
                    logger.info(
                        "session regime tag computed: tag=%s tagger=%s",
                        tag,
                        tagger,
                    )
            if not is_session_regime_allowed(self._session_regime_tag):
                return None  # blocker: entry_regime_tag (per-tick summary)

        # Optional trap gate: require a minimum number of trap cues to be present
        # (any side) before considering entries. Disabled by default.
        if as_bool(os.getenv("ENTRY_TRAP_GATE_ENABLED", "false")):
            min_match = 0
            try:
                min_match = int(os.getenv("ENTRY_TRAP_MIN_MATCH", "2") or "2")
            except Exception:
                min_match = 2
            # Reuse shadow scorer basis; parse fired signal list from basis string.
            # Basis format: "multi_signal_ce(score=...:sig1,sig2,...)" or similar
            _dir_hint, _basis, _shadow_score = self._shadow_direction_from_snapshot(snap)
            fired_str = _basis.split(":", 1)[-1] if ":" in _basis else _basis
            fired_set = {s.strip() for s in fired_str.split(",") if s.strip()}
            # CE-side trap cues
            ce_traps = {"orb_low_rejected", "vwap_reclaim_bull", "pe_iv_fading"}
            # PE-side trap cues
            pe_traps = {"orb_high_rejected", "vwap_reject_bear", "ce_iv_fading"}
            ce_hits = len(fired_set & ce_traps)
            pe_hits = len(fired_set & pe_traps)
            if max(ce_hits, pe_hits) < max(1, min_match):
                return None  # blocker: trap_gate (per-tick summary)

        # Brain gate: DayScore + consensus (skipped for ML-entry-primary profile when configured).
        skip_brain = (
            self._strategy_profile_id in _PROFILES_ML_ENTRY_DET_DIRECTION
            and as_bool(os.getenv("ML_ENTRY_DET_SKIP_BRAIN_GATE", "false"))
        )
        if not skip_brain:
            entry_votes_for_brain = [
                v for v in votes
                if v.signal_type == SignalType.ENTRY and v.direction in (Direction.CE, Direction.PE)
            ]
            brain_decision = self._brain.gate_entry(entry_votes_for_brain, self._day_context)
            if not brain_decision.allowed:
                return None  # blocker: brain_gate:<reason> (per-tick summary)

        # ── Trader discipline gates (actual blocking) ────────────────────────
        # 0. Minimum re-entry spacing: never enter immediately after any exit.
        _reentry_gap = int(os.getenv("MIN_REENTRY_BARS", "3"))
        if self._last_any_exit_bar is not None:
            _bars_since_exit = self._session_event_count - self._last_any_exit_bar
            if _bars_since_exit < _reentry_gap:
                return None  # blocker: min_reentry_gap (per-tick summary)

        # 1. SIDEWAYS + returns_mixed: no directional conviction — skip entirely.
        regime_str = str(
            regime_signal.regime.value if hasattr(regime_signal.regime, "value") else regime_signal.regime or ""
        ).upper()
        if (
            regime_str == "SIDEWAYS"
            and "returns_mixed" in (regime_signal.reason or "")
        ):
            return None  # blocker: sideways_returns_mixed (per-tick summary)

        # 2. Post-STOP_LOSS cooldown: no re-entry for N bars after a hard stop.
        _stop_cool = int(os.getenv("STOP_LOSS_COOLDOWN_BARS", "5"))
        if self._last_stop_bar is not None:
            _bars_since = self._session_event_count - self._last_stop_bar
            if _bars_since < _stop_cool:
                return None  # blocker: stop_loss_cooldown (per-tick summary)

        # 3. Direction-flip block: after a STOP_LOSS, don't flip direction for N bars.
        _flip_cool = int(os.getenv("DIRECTION_FLIP_COOLDOWN_BARS", "8"))
        if self._last_stop_bar is not None and self._last_exit_direction:
            _bars_since = self._session_event_count - self._last_stop_bar
            if _bars_since < _flip_cool:
                _cur_entry_dirs = {
                    str(v.direction.value if hasattr(v.direction, "value") else v.direction or "").upper()
                    for v in votes
                    if v.signal_type == SignalType.ENTRY and v.direction in (Direction.CE, Direction.PE)
                }
                if self._last_exit_direction not in _cur_entry_dirs:
                    return None  # blocker: direction_flip_cooldown (per-tick summary)

        # 4. Zero-MFE same-direction block: if the last trade produced no favorable
        #    excursion (mfe ≈ 0) and was a loss, the thesis was immediately wrong.
        #    Block the same direction for ZERO_MFE_COOLDOWN_BARS — the market is
        #    telling you the move isn't happening. Default: 10 bars (~10 min).
        _zero_mfe_cool = int(os.getenv("ZERO_MFE_COOLDOWN_BARS", "10"))
        if self._last_zero_mfe_bar is not None and self._last_zero_mfe_direction:
            _bars_since_zero = self._session_event_count - self._last_zero_mfe_bar
            if _bars_since_zero < _zero_mfe_cool:
                _cur_dirs = {
                    str(v.direction.value if hasattr(v.direction, "value") else v.direction or "").upper()
                    for v in votes
                    if v.signal_type == SignalType.ENTRY and v.direction in (Direction.CE, Direction.PE)
                }
                if self._last_zero_mfe_direction in _cur_dirs:
                    return None  # blocker: zero_mfe_cooldown (per-tick summary)
        # 5. Direction-evidence agreement gate.
        #    The regime tagger computes bull_score/bear_score from returns, volume,
        #    OI, PCR and ORB — rich market evidence that is ALREADY COMPUTED but was
        #    never used to gate the direction of a proposed trade.
        #
        #    The gate asks: "does the current market evidence support the direction
        #    we're about to trade?" If bear_score=0 and bull_score=0.8, entering PE
        #    is trading against the observable evidence regardless of what the ML
        #    timing model says.
        #
        #    Block when:
        #      PE entry: bull_score > OPPOSING_MAX  AND  bear_score < SUPPORT_MIN
        #      CE entry: bear_score > OPPOSING_MAX  AND  bull_score < SUPPORT_MIN
        #
        #    Defaults are conservative — only veto when evidence is clearly wrong.
        _ev_support_min  = float(os.getenv("DIRECTION_EVIDENCE_SUPPORT_MIN", "0.2"))
        _ev_opposing_max = float(os.getenv("DIRECTION_EVIDENCE_OPPOSING_MAX", "0.6"))
        _evidence = getattr(regime_signal, "evidence", None) or {}
        _bull = float(_evidence.get("bull_score", -1))
        _bear = float(_evidence.get("bear_score", -1))
        if _bull >= 0 and _bear >= 0:
            _entry_dirs = {
                str(v.direction.value if hasattr(v.direction, "value") else v.direction or "").upper()
                for v in votes
                if v.signal_type == SignalType.ENTRY and v.direction in (Direction.CE, Direction.PE)
            }
            if "PE" in _entry_dirs and _bull > _ev_opposing_max and _bear < _ev_support_min:
                return None  # blocker: direction_evidence_mismatch:PE (per-tick summary)
            if "CE" in _entry_dirs and _bear > _ev_opposing_max and _bull < _ev_support_min:
                return None  # blocker: direction_evidence_mismatch:CE (per-tick summary)

        # 6. Trend-fade guard (#3): don't fade the dominant VWAP trend on a shallow
        #    pullback. No-op unless TREND_FADE_GUARD_ENABLED. Mirrored in _derive_entry_blocker.
        if self._trend_fade_block(votes, snap) is not None:
            return None  # blocker: trend_fade_guard:<dir> (per-tick summary)
        # ── End discipline gates ──────────────────────────────────────────────

        avoid_votes = [vote for vote in votes if vote.direction == Direction.AVOID]
        if avoid_votes:
            return None  # blocker: avoid_veto (per-tick summary)

        entry_votes = [
            vote
            for vote in votes
            if vote.signal_type == SignalType.ENTRY and vote.direction in (Direction.CE, Direction.PE)
        ]
        if not entry_votes:
            return None

        use_ml_det_dir = self._strategy_profile_id in _PROFILES_ML_ENTRY_DET_DIRECTION
        if use_ml_det_dir and not any(vote.strategy_name == "ML_ENTRY" for vote in entry_votes):
            return None  # blocker: ml_timing_gate (per-tick summary)

        # ML_ENTRY's positive vote is the primary entry signal — keep it in the
        # pool as a first-class voter alongside any rule strategies that fired.
        # Rule strategies with higher confidence can still win the slot via the
        # ranking below; ML wins when rule strategies are silent. Existing
        # vetoes still eliminate bad trades downstream:
        #   - AVOID votes (already handled above)
        #   - direction conflict (rule strategies explicitly disagreeing)
        #   - regime_signal.confidence < 0.60
        #   - risk pause / invalid entry phase / session trade cap
        # "Silence ≠ veto": a rule strategy with no setup detected no longer
        # blocks a high-conviction ML signal.
        vote_pool = list(entry_votes)

        # ORB wide-range gate: skip ORB-family entries when opening range is
        # too wide. Does not affect ML_ENTRY (it is not an ORB strategy).
        orb_max = self._run_risk_config.orb_max_range_pts
        if orb_max and orb_max > 0:
            or_width = snap.or_width
            if or_width is not None and float(or_width) > float(orb_max):
                pre_filter = len(vote_pool)
                vote_pool = [
                    v for v in vote_pool
                    if v.strategy_name not in ("ORB", "ORB_RETEST", "HIGH_VOL_ORB")
                ]
                if len(vote_pool) < pre_filter:
                    logger.debug(
                        "ORB entry gated: or_width=%.0f > orb_max_range_pts=%.0f",
                        float(or_width),
                        float(orb_max),
                    )

        # In use_ml_det_dir mode, ML_ENTRY is guaranteed in vote_pool (we
        # returned above if it didn't vote). The legacy DET_DIRECTION fallback
        # is therefore unreachable and has been removed; ML_ENTRY itself
        # carries a direction via _resolve_direction in ml_entry.py.
        if not vote_pool:
            return None
        entry_votes = vote_pool

        if self._entry_pipeline_v2:
            return self._process_entry_votes_v2(
                votes=entry_votes,
                snap=snap,
                risk=risk,
                regime_signal=regime_signal,
            )

        if self._strategy_profile_id in _PROFILES_ML_ENTRY_CONSENSUS:
            return self._process_entry_consensus(
                entry_votes=entry_votes,
                snap=snap,
                risk=risk,
                regime_signal=regime_signal,
            )

        ce_votes = [vote for vote in entry_votes if vote.direction == Direction.CE]
        pe_votes = [vote for vote in entry_votes if vote.direction == Direction.PE]
        has_direction_conflict = bool(ce_votes and pe_votes)
        if has_direction_conflict and self._strategy_profile_id in _PROFILES_ML_ENTRY_DET_DIRECTION:
            entry_votes = self._resolve_direction_conflict_deterministic(entry_votes)
            ce_votes = [vote for vote in entry_votes if vote.direction == Direction.CE]
            pe_votes = [vote for vote in entry_votes if vote.direction == Direction.PE]
            has_direction_conflict = bool(ce_votes and pe_votes)
        ml_can_resolve_direction_conflict = has_direction_conflict and self._entry_policy_can_resolve_direction_conflict()
        if has_direction_conflict and not ml_can_resolve_direction_conflict:
            return None  # blocker: direction_conflict (per-tick summary)

        if (
            self._strategy_profile_id not in _PROFILES_RELAX_REGIME_CONF
            and regime_signal.confidence < 0.60
        ):
            return None  # blocker: regime_confidence (per-tick summary)

        if not snap.is_valid_entry_phase or self._risk.is_paused:
            return None  # blocker: entry_phase / risk_pause (per-tick summary)
        ranked_entry_votes = sorted(entry_votes, key=lambda item: item.confidence, reverse=True)
        if ml_can_resolve_direction_conflict:
            scored_candidates: list[tuple[StrategyVote, EntryPolicyDecision]] = []
            for candidate in ranked_entry_votes:
                self._apply_strike_selection(candidate, snap, regime=regime_signal.regime.value)
                policy_decision = self._evaluate_entry_policy(candidate, snap, regime_signal, risk)
                self._annotate_policy(candidate, policy_decision)
                self._annotate_vote_contract(candidate)
                scored_candidates.append((candidate, policy_decision))
            eligible = [
                (candidate, policy_decision)
                for candidate, policy_decision in scored_candidates
                if candidate.confidence >= self._min_confidence
                and policy_decision.allowed
                and not candidate.raw_signals.get("_strike_vetoed")
            ]
            if not eligible:
                return None  # blocker: ml_direction_resolution (per-tick summary)
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
            self._apply_strike_selection(candidate, snap, regime=regime_signal.regime.value)
            policy_decision = self._evaluate_entry_policy(candidate, snap, regime_signal, risk)
            self._annotate_policy(candidate, policy_decision)
            self._annotate_vote_contract(candidate)
            if candidate.confidence < self._min_confidence:
                continue  # blocker: confidence_gate (per-tick summary)
            if candidate.raw_signals.get("_strike_vetoed"):
                continue  # blocker: strike_vetoed (per-tick summary)
            if not policy_decision.allowed:
                continue  # blocker: policy_gate (per-tick summary)
            return self._build_entry_signal(candidate, snap, risk, entry_votes, regime_signal, policy_decision)
        return None

    def _process_entry_consensus(
        self,
        *,
        entry_votes: list[StrategyVote],
        snap: SnapshotAccessor,
        risk: RiskContext,
        regime_signal: RegimeSignal,
    ) -> Optional[TradeSignal]:
        ml_votes = [v for v in entry_votes if v.strategy_name == "ML_ENTRY"]
        if not ml_votes:
            return None  # blocker: ml_timing_gate / consensus_no_ml_entry_vote (per-tick summary)
        ml_vote = max(ml_votes, key=lambda v: float(v.confidence or 0))
        # E3-S1: bypass path uses its own threshold, aligned to the entry gate (0.65).
        # CONSENSUS_BYPASS_MIN_CONFIDENCE can be raised independently of the main
        # self._min_confidence (0.50) which guards non-consensus paths.
        _bypass_min = float(os.getenv("CONSENSUS_BYPASS_MIN_CONFIDENCE", str(self._min_confidence)) or self._min_confidence)
        if ml_vote.confidence < _bypass_min:
            return None  # blocker: consensus_bypass_confidence (per-tick summary)

        shadow_dir, shadow_basis, shadow_score = self._shadow_direction_from_snapshot(snap)
        hint_dir, ce_prob = extract_ml_direction_hint(ml_vote)
        rule_votes = [
            v
            for v in entry_votes
            if v.strategy_name != "ML_ENTRY" and v.direction in (Direction.CE, Direction.PE)
        ]
        consensus = resolve_direction_consensus(
            snap=snap,
            rule_votes=rule_votes,
            shadow_direction=shadow_dir,
            shadow_score=shadow_score,
            ml_direction_hint=hint_dir,
            ml_ce_prob=ce_prob,
            regime_signal=regime_signal,
        )
        if consensus.vetoed or consensus.direction is None:
            return None  # blocker: direction_consensus:<reason> (per-tick summary)

        _consensus_extras: dict[str, Any] = {
            "direction_source": "direction_consensus",
            "direction_consensus_ce": round(consensus.ce_score, 3),
            "direction_consensus_pe": round(consensus.pe_score, 3),
            "direction_consensus_margin": round(consensus.margin, 3),
            "direction_consensus_shadow_basis": shadow_basis,
            "direction_consensus_sources": {k: round(v, 3) for k, v in (consensus.sources or {}).items()},
        }
        # Mutate the original ml_vote so _entry_candidate_gate_rows (which receives
        # the original vote from the engine's vote pool) can see the direction data.
        if isinstance(ml_vote.raw_signals, dict):
            ml_vote.raw_signals.update(_consensus_extras)
        else:
            ml_vote.raw_signals = dict(_consensus_extras)

        trade_vote = StrategyVote(
            strategy_name=ml_vote.strategy_name,
            snapshot_id=ml_vote.snapshot_id,
            timestamp=ml_vote.timestamp,
            trade_date=ml_vote.trade_date,
            signal_type=SignalType.ENTRY,
            direction=consensus.direction,
            confidence=ml_vote.confidence,
            reason=f"ml_entry+consensus: {consensus.direction.value} margin={consensus.margin:.2f}",
            raw_signals={
                **(ml_vote.raw_signals if isinstance(ml_vote.raw_signals, dict) else {}),
                "_entry_policy_mode": "bypass",
            },
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=(
                snap.atm_ce_close
                if consensus.direction == Direction.CE
                else snap.atm_pe_close
            ),
        )
        if self._run_risk_config.atm_strike_only and not self._allow_non_atm_for_ml_entry(trade_vote):
            trade_vote = self._force_atm_strike(trade_vote, snap)

        if (
            self._strategy_profile_id not in _PROFILES_RELAX_REGIME_CONF
            and regime_signal.confidence < 0.60
        ):
            return None
        if not snap.is_valid_entry_phase or self._risk.is_paused:
            return None

        self._apply_strike_selection(trade_vote, snap, regime=regime_signal.regime.value)
        if trade_vote.raw_signals.get("_strike_vetoed"):
            logger.debug(
                "consensus entry blocked: strike_veto reason=%s",
                trade_vote.raw_signals.get("_strike_veto_reason"),
            )
            return None
        if (
            self._run_risk_config.atm_strike_only
            and not self._allow_non_atm_for_ml_entry(trade_vote)
            and not self._is_atm_strike(snap, trade_vote)
        ):
            return None  # blocker: consensus_otm_strike_policy (per-tick summary)

        policy_decision = self._evaluate_entry_policy(trade_vote, snap, regime_signal, risk)
        self._annotate_policy(trade_vote, policy_decision)
        # Mirror annotation back to ml_vote so the candidate trace reflects the real outcome.
        # Without this, ml_vote._policy_allowed stays None and the trace shows "blocked" even
        # though the consensus bypass passed — a tracing lie.
        if isinstance(ml_vote.raw_signals, dict):
            ml_vote.raw_signals["_policy_allowed"] = policy_decision.allowed
            ml_vote.raw_signals["_policy_reason"] = policy_decision.reason
            ml_vote.raw_signals["_policy_score"] = round(policy_decision.score, 3)
            ml_vote.raw_signals["_policy_checks"] = dict(policy_decision.checks)
            ml_vote.raw_signals["_execution_path"] = "consensus_bypass"
        self._annotate_vote_contract(trade_vote)
        if not policy_decision.allowed:
            return None
        # Grade + tier the FINAL trade vote (now carries direction_consensus_* or
        # entry_dir_*). _collect_votes graded the raw ML_ENTRY vote earlier, but the
        # consensus direction is only resolved here, so re-grade with full context.
        self._grade_and_tier_vote(trade_vote, snap, regime_signal.regime.value, risk)
        return self._build_entry_signal(
            trade_vote, snap, risk, entry_votes, regime_signal, policy_decision
        )

    # ------------------------------------------------------------------
    # Entry pipeline v2 — gate cascade
    # ------------------------------------------------------------------

    def _process_entry_votes_v2(
        self,
        *,
        votes: list[StrategyVote],
        snap: SnapshotAccessor,
        risk: RiskContext,
        regime_signal: RegimeSignal,
    ) -> Optional[TradeSignal]:
        """Run the v2 gate-cascade pipeline (STRATEGY_ENTRY_PIPELINE_V2=1)."""
        relax_conf = self._strategy_profile_id in _PROFILES_RELAX_REGIME_CONF
        is_consensus = self._strategy_profile_id in _PROFILES_ML_ENTRY_CONSENSUS
        gates = build_entry_pipeline(
            regime_min_relax=relax_conf,
            is_consensus=is_consensus,
            shadow_fn=self._shadow_direction_from_snapshot,
            ml_hint_fn=extract_ml_direction_hint,
            consensus_fn=resolve_direction_consensus,
            ml_entry_vote_selector=lambda vts: max(
                (v for v in vts if v.strategy_name == "ML_ENTRY"),
                key=lambda v: float(v.confidence or 0),
                default=None,
            ),
            apply_strike_fn=lambda vote, snap_, regime="": self._apply_strike_selection(
                vote, snap_, regime=regime
            ),
            policy_fn=lambda vote, snap_, regime_, risk_: self._evaluate_entry_policy(
                vote, snap_, regime_, risk_
            ),
        )
        ctx = EntryContext(
            snap=snap,
            regime=regime_signal,
            risk=risk,
            votes=votes,
            config=self._entry_config,
        )
        all_votes = votes

        def _build(ctx_: EntryContext) -> Optional[TradeSignal]:
            if ctx_.candidate is None or ctx_.direction is None:
                return None
            policy_decision = self._evaluate_entry_policy(
                ctx_.candidate, ctx_.snap, ctx_.regime, ctx_.risk
            )
            self._annotate_vote_contract(ctx_.candidate)
            return self._build_entry_signal(
                ctx_.candidate, ctx_.snap, ctx_.risk,
                all_votes, ctx_.regime, policy_decision,
            )

        signal = _evaluate_v2(ctx=ctx, gates=gates, build_signal_fn=_build)
        # Capture the gate cascade for this bar so the sim/Terminal can show
        # exactly how the trade was (or wasn't) picked. Cheap, structural — no
        # behaviour change. Reason codes come straight from each GateResult.
        ts = getattr(snap, "timestamp", None)
        self.last_entry_trace = {
            "decision_id": ctx.decision_id,
            "snapshot_id": getattr(snap, "snapshot_id", None),
            "timestamp": ts.isoformat() if ts is not None else None,
            "final_outcome": "entered" if signal is not None else "no_trade",
            "selected_direction": (ctx.direction.value if ctx.direction is not None else None),
            "selected_strike": ctx.strike,
            "selected_premium": ctx.premium,
            "primary_blocker_gate": next(
                (t.gate_name for t in reversed(ctx.trace) if t.outcome.value != "pass"),
                None,
            ),
            "gates": [
                {
                    "gate": t.gate_name,
                    "outcome": t.outcome.value,
                    "reason": t.reason,
                    "values": dict(t.values),
                }
                for t in ctx.trace
            ],
        }
        return signal

    @staticmethod
    def _force_atm_strike(vote: StrategyVote, snap: SnapshotAccessor) -> StrategyVote:
        atm = snap.atm_strike
        if atm is None or int(atm) <= 0 or vote.direction not in (Direction.CE, Direction.PE):
            return vote
        vote.proposed_strike = int(atm)
        premium = snap.option_ltp(vote.direction.value, int(atm))
        if premium is not None and premium > 0:
            vote.proposed_entry_premium = float(premium)
        vote.raw_signals["_strike_policy"] = "atm_only"
        return vote

    def _is_atm_strike(self, snap: SnapshotAccessor, vote: StrategyVote) -> bool:
        atm = snap.atm_strike
        strike = vote.proposed_strike
        if atm is None or strike is None:
            return False
        return int(strike) == int(atm)

    def _allow_non_atm_for_ml_entry(self, vote: StrategyVote) -> bool:
        return bool(
            self._run_risk_config.allow_non_atm_for_ml_entry
            and str(vote.strategy_name or "").strip().upper() == "ML_ENTRY"
        )

    @staticmethod
    def _resolve_direction_conflict_deterministic(entry_votes: list[StrategyVote]) -> list[StrategyVote]:
        ce_votes = [vote for vote in entry_votes if vote.direction == Direction.CE]
        pe_votes = [vote for vote in entry_votes if vote.direction == Direction.PE]
        if ce_votes and not pe_votes:
            return ce_votes
        if pe_votes and not ce_votes:
            return pe_votes
        if len(ce_votes) >= len(pe_votes):
            return ce_votes
        return pe_votes

    def _deterministic_direction_vote(self, snap: SnapshotAccessor) -> StrategyVote:
        direction, basis, _ = self._shadow_direction_from_snapshot(snap)
        premium = snap.atm_ce_close if direction == Direction.CE else snap.atm_pe_close
        return StrategyVote(
            strategy_name="DET_DIRECTION",
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=direction,
            confidence=0.50,
            reason=f"det_direction: {basis}",
            raw_signals={"direction_source": basis},
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=premium,
        )

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

        # Live-only entry gate (single chokepoint for ALL entry paths): when this
        # engine instance is the LIVE book (ENTRY_LIVE_ONLY_GATE=1), only spend its
        # one slot on live-eligible (GOOD) entries, so a low-grade trade never
        # occupies the slot and blocks a later GOOD trade. Ensure the vote is graded
        # first (consensus path grades earlier; direct paths may not have). The
        # PAPER book runs the same engine with the gate OFF and takes everything.
        if self._entry_live_only_gate:
            raw = best_vote.raw_signals if isinstance(best_vote.raw_signals, dict) else {}
            if "live_would_take" not in raw:
                self._grade_and_tier_vote(best_vote, snap, regime_signal.regime.value, risk)
                raw = best_vote.raw_signals if isinstance(best_vote.raw_signals, dict) else {}
            if not bool(raw.get("live_would_take")):
                logger.debug("live-gate skip: grade=%s tier=%s",
                             raw.get("entry_grade"), raw.get("tier"))
                return None

        # Strike layer veto is authoritative — no affordable/priced strike = no trade.
        if best_vote.raw_signals.get("_strike_vetoed"):
            logger.info(
                "entry skipped: strike_veto reason=%s",
                best_vote.raw_signals.get("_strike_veto_reason"),
            )
            return None

        selected_strike = best_vote.proposed_strike or snap.atm_strike
        if selected_strike is None or int(selected_strike) <= 0:
            return None
        selected_strike = int(selected_strike)
        if self._run_risk_config.atm_strike_only and not self._allow_non_atm_for_ml_entry(best_vote):
            atm = snap.atm_strike
            if atm is None or int(selected_strike) != int(atm):
                logger.info("entry blocked: atm_strike_only policy strike=%s atm=%s", selected_strike, atm)
                return None

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
        raw = best_vote.raw_signals if isinstance(best_vote.raw_signals, dict) else {}
        underlying_stop_pct: Optional[float] = None
        underlying_target_pct: Optional[float] = None
        playbook_metrics: Optional[dict[str, Any]] = None
        if raw.get("_playbook_brain"):
            position_side = "SHORT"
            max_hold_bars = int(raw.get("_max_hold_bars") or 45)
            raw_underlying = raw.get("_underlying_stop_pct")
            if raw_underlying is not None:
                underlying_stop_pct = float(raw_underlying)
            playbook_raw = raw.get(PLAYBOOK_EXIT_KEY)
            if isinstance(playbook_raw, dict):
                playbook_metrics = dict(playbook_raw)
        elif raw.get("_r1s_short_ce"):
            position_side = "SHORT"
            max_hold_bars = 20
        elif raw.get("_debit_long_option"):
            position_side = "LONG"
            max_hold_bars = 20
        else:
            position_side = "LONG"
            max_hold_bars = None

        # Apply profile-level underlying stop/target (overridden by strategy raw if set).
        cfg_underlying = self._run_risk_config
        if underlying_stop_pct is None and cfg_underlying.underlying_stop_pct is not None:
            underlying_stop_pct = float(cfg_underlying.underlying_stop_pct)
        if underlying_target_pct is None and cfg_underlying.underlying_target_pct is not None:
            underlying_target_pct = float(cfg_underlying.underlying_target_pct)
        signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            timestamp=snap.timestamp_or_now,
            snapshot_id=snap.snapshot_id,
            signal_type=SignalType.ENTRY,
            direction=direction.value,
            strike=selected_strike,
            expiry=expiry,
            entry_premium=premium,
            position_side=position_side,
            max_hold_bars=max_hold_bars,
            stop_loss_pct=stop_loss_pct,
            target_pct=target_pct,
            underlying_stop_pct=underlying_stop_pct,
            underlying_target_pct=underlying_target_pct,
            stagnant_exit_bars=cfg_underlying.stagnant_exit_bars,
            stagnant_min_gain_pct=cfg_underlying.stagnant_min_gain_pct,
            stagnant_exit_condition=str(cfg_underlying.stagnant_exit_condition or ""),
            thesis_fail_exit_bars=cfg_underlying.thesis_fail_exit_bars,
            thesis_fail_min_mfe_pct=cfg_underlying.thesis_fail_min_mfe_pct,
            thesis_fail_pnl_pct=cfg_underlying.thesis_fail_pnl_pct,
            early_stop_loss_bars=cfg_underlying.early_stop_loss_bars,
            early_stop_loss_pct=float(cfg_underlying.early_stop_loss_pct or 0.0),
            playbook_exit_policy=playbook_metrics,
            trailing_enabled=(
                False
                if position_side == "SHORT" or raw.get("_playbook_brain")
                else trailing_cfg.trailing_enabled
            ),
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
            raw_signals=dict(raw) if isinstance(raw, dict) else {},
        )
        entry_decision_mode = self._decision_mode_from_policy(policy_decision)
        entry_metrics: dict[str, Any] = {
            "confidence": float(combined_confidence),
            "policy_score": (float(policy_decision.score) if policy_decision is not None else None),
        }
        if playbook_metrics is not None:
            entry_metrics[PLAYBOOK_EXIT_KEY] = playbook_metrics
        self._annotate_signal_contract(
            signal,
            decision_mode=entry_decision_mode,
            decision_reason_code=self._reason_code_from_policy(policy_decision),
            decision_metrics=entry_metrics,
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
        # Record stop-loss exits so discipline rules can enforce cooldowns.
        exit_reason_str = str(getattr(exit_signal, "exit_reason", None) or "").upper()
        exit_dir = str(
            position.direction.value if hasattr(position.direction, "value")
            else position.direction or ""
        ).upper()
        # Always track any exit for minimum re-entry spacing.
        self._last_any_exit_bar = self._session_event_count
        # Zero-MFE exit: market never moved in our favour — same direction re-entry
        # is almost certainly wrong (the thesis produced no excursion whatsoever).
        # Block same-direction entries for ZERO_MFE_COOLDOWN_BARS after this.
        _ZERO_MFE_THRESHOLD = 0.001  # <0.1% MFE counts as zero
        if position.mfe_pct < _ZERO_MFE_THRESHOLD and position.pnl_pct < 0:
            self._last_zero_mfe_bar = self._session_event_count
            self._last_zero_mfe_direction = exit_dir
            logger.info(
                "zero_mfe_exit dir=%s mfe=%.3f pnl=%.3f — same-direction cooldown armed",
                exit_dir, position.mfe_pct, position.pnl_pct,
            )
        if "STOP" in exit_reason_str and "TIME" not in exit_reason_str:
            # Hard STOP_LOSS: longer cooldown + direction-flip block.
            self._last_stop_bar = self._session_event_count
            self._last_exit_direction = exit_dir
        elif "TIME" in exit_reason_str and position.pnl_pct < 0:
            # TIME_STOP with a loss: treat like a soft stop — record direction too.
            self._last_stop_bar = self._session_event_count
            self._last_exit_direction = exit_dir
        self._risk.record_trade_result(
            pnl_pct=position.pnl_pct,
            lots=position.lots,
            entry_premium=position.entry_premium,
        )
        self._brain.on_trade_result(
            pnl_pct=position.pnl_pct,
            strategy_name=str(position.entry_strategy or ""),
        )
        self._log.log_position_close(exit_signal=exit_signal, position=position)

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

    def _evaluate_entry_policy(
        self,
        vote: StrategyVote,
        snap: SnapshotAccessor,
        regime_signal: RegimeSignal,
        risk: RiskContext,
    ) -> EntryPolicyDecision:
        mode = str(vote.raw_signals.get("_entry_policy_mode") or "").strip().lower()
        if mode == "bypass":
            checks = {"mode": "bypass", "strategy": vote.strategy_name}
            return EntryPolicyDecision.allow("bypass:strategy_owned", score=1.0, checks=checks)
        if mode == "advisory":
            decision = self._entry_policy.evaluate(snap, vote, regime_signal, risk)
            return EntryPolicyDecision.allow(
                f"advisory:{decision.reason}",
                score=max(0.75, float(decision.score)),
                checks={**decision.checks, "mode": "advisory"},
                adjustments=decision.adjustments,
            )
        return self._entry_policy.evaluate(snap, vote, regime_signal, risk)

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
        direction, basis, _ = self._shadow_direction_from_snapshot(snap)
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

    def _shadow_direction_from_snapshot(self, snap: SnapshotAccessor) -> tuple[Direction, str, float]:
        """Multi-signal direction scorer.

        Each signal contributes a signed score (positive = CE/bullish, negative = PE/bearish).
        Weights reflect reliability from OOS analysis:
          1. Opening range breakout/breakdown  — strongest structural signal (weight 2)
          2. Price vs VWAP                     — intraday trend bias        (weight 2)
          3. ATM CE/PE premium momentum        — option market revealing hand (weight 2)
          4. PCR change momentum               — option flow pressure        (weight 1)
          5. Futures return 15m                — medium-term momentum        (weight 1)
          6. Futures return 5m                 — short-term momentum (last)  (weight 1)
          7. VIX intraday change               — top ML predictor            (weight 1.5)
          8. IV skew PE vs CE                  — downside risk pricing       (weight 1)
          9–14. Trap detection signals         — failed breakout/VWAP/IV fade (weight 2 each)

        Final direction = CE if score > 0, PE if score < 0, tie-breaks to 15m then 5m.
        Basis string records which signals fired (for logs/diagnostics).
        """
        score: float = 0.0
        fired: list[str] = []

        # 1. Opening range breakout/breakdown (weight 2) — confirmed structural break
        if snap.orh_broken:
            score += 2.0
            fired.append("orh_broken")
        elif snap.orl_broken:
            score -= 2.0
            fired.append("orl_broken")
        elif snap.or_ready:
            # Not yet broken: price position relative to OR mid gives a softer signal
            pvs_orh = snap.price_vs_orh
            pvs_orl = snap.price_vs_orl
            if pvs_orh is not None and pvs_orl is not None:
                or_bias = float(pvs_orh) - float(pvs_orl)
                if or_bias > 0:
                    score += 0.5
                    fired.append("or_upper_half")
                elif or_bias < 0:
                    score -= 0.5
                    fired.append("or_lower_half")

        # 2. Price vs VWAP (weight 2) — intraday trend bias
        pvwap = snap.price_vs_vwap
        if pvwap is not None and abs(float(pvwap)) > 0.0:
            score += 2.0 if float(pvwap) > 0 else -2.0
            fired.append("above_vwap" if float(pvwap) > 0 else "below_vwap")

        # 3. ATM CE/PE premium momentum (weight 2) — which option market is bidding up
        ce_p = snap.atm_ce_close
        pe_p = snap.atm_pe_close
        if ce_p and pe_p and ce_p > 0 and pe_p > 0:
            ratio = ce_p / pe_p
            if ratio > 1.04:
                score += 2.0
                fired.append("ce_prem_dominant")
            elif ratio < 0.96:
                score -= 2.0
                fired.append("pe_prem_dominant")

        # 4. PCR change 5m (weight 1) — option flow pressure
        # PCR rising = more put OI added = market positioning bearish = PE signal
        # PCR falling = more call OI added = CE signal
        pcr_chg = snap.pcr_change_5m
        if pcr_chg is not None and float(pcr_chg) != 0.0:
            score += 1.0 if float(pcr_chg) < 0 else -1.0
            fired.append("pcr_falling" if float(pcr_chg) < 0 else "pcr_rising")

        # 5. Futures return 15m (weight 1) — medium-term momentum
        r15 = snap.fut_return_15m
        if r15 is not None and float(r15) != 0.0:
            score += 1.0 if float(r15) > 0 else -1.0
            fired.append("r15m_up" if float(r15) > 0 else "r15m_dn")

        # 6. Futures return 5m (weight 1) — short-term momentum
        r5 = snap.fut_return_5m
        if r5 is not None and float(r5) != 0.0:
            score += 1.0 if float(r5) > 0 else -1.0
            fired.append("r5m_up" if float(r5) > 0 else "r5m_dn")

        # 7. VIX intraday change (weight 1.5) — top ML direction predictor (abs_corr 0.068).
        # vix_intraday_chg is a percentage: +7.0 means VIX rose 7% intraday.
        # Rising VIX = fear building = bearish; falling VIX = fear receding = bullish.
        vix_chg = snap.vix_intraday_chg
        if vix_chg is not None and abs(float(vix_chg)) >= 3.0:
            score += -1.5 if float(vix_chg) > 0 else 1.5
            fired.append("vix_rising" if float(vix_chg) > 0 else "vix_falling")

        # 8. IV skew: PE vs CE implied vol (weight 1).
        # PE IV dominating means the options market is pricing downside risk.
        atm_ce_iv = snap.atm_ce_iv
        atm_pe_iv = snap.atm_pe_iv
        if atm_ce_iv and atm_pe_iv and float(atm_ce_iv) > 0 and float(atm_pe_iv) > 0:
            iv_ratio = float(atm_pe_iv) / float(atm_ce_iv)
            if iv_ratio > 1.10:
                score -= 1.0
                fired.append("pe_iv_dom")
            elif iv_ratio < 0.90:
                score += 1.0
                fired.append("ce_iv_dom")

        # ── Trap detection signals (E5-S1, weight 2 each) ──────────────────────
        # These detect when a breakout/breakdown has failed, forcing the trapped
        # side to cover — the strongest intraday setup in Indian option premium.

        # 9. ORB low rejected: price broke below ORB low but recovered above it.
        #    Trapped sellers forced to buy back → bullish squeeze.
        if snap.or_ready and snap.orl_broken:
            orl = snap.orl
            fut = snap.fut_close
            if orl is not None and fut is not None and float(fut) > float(orl):
                score += 2.0
                fired.append("orb_low_rejected")

        # 10. ORB high rejected: price broke above ORB high but fell back below.
        #     Trapped buyers forced to sell → bearish squeeze.
        if snap.or_ready and snap.orh_broken:
            orh = snap.orh
            fut = snap.fut_close
            if orh is not None and fut is not None and float(fut) < float(orh):
                score -= 2.0
                fired.append("orb_high_rejected")

        # 11. VWAP reclaim (bullish): price was below VWAP last bar, now above.
        #     Confirms trapped sellers covering as market reclaims intraday anchor.
        if len(self._pvwap_buf) >= 2:
            prev_pvwap = self._pvwap_buf[-2]
            cur_pvwap = self._pvwap_buf[-1]
            if prev_pvwap is not None and cur_pvwap is not None:
                if float(prev_pvwap) < 0 < float(cur_pvwap):
                    score += 2.0
                    fired.append("vwap_reclaim_bull")

        # 12. VWAP rejection (bearish): price was above VWAP last bar, now below.
        #     Confirms trapped buyers cutting as market loses intraday anchor.
        if len(self._pvwap_buf) >= 2:
            prev_pvwap = self._pvwap_buf[-2]
            cur_pvwap = self._pvwap_buf[-1]
            if prev_pvwap is not None and cur_pvwap is not None:
                if float(prev_pvwap) > 0 > float(cur_pvwap):
                    score -= 2.0
                    fired.append("vwap_reject_bear")

        # 13. PE IV fading (CE signal): PE IV spiked 2 bars ago then compressed.
        #     Put buyers paid up in panic, now IV is collapsing — sellers winning.
        if len(self._iv_buf) >= 3:
            ce0, pe0 = self._iv_buf[0]  # oldest (3 bars ago)
            ce1, pe1 = self._iv_buf[1]  # 2 bars ago
            ce2, pe2 = self._iv_buf[2]  # current
            if pe0 and pe1 and pe2 and float(pe0) > 0 and float(pe1) > 0 and float(pe2) > 0:
                if float(pe1) > float(pe0) * 1.05 and float(pe2) < float(pe1) * 0.97:
                    score += 2.0
                    fired.append("pe_iv_fading")

        # 14. CE IV fading (PE signal): CE IV spiked 2 bars ago then compressed.
        #     Call buyers paid up in panic, now IV is collapsing — sellers winning.
        if len(self._iv_buf) >= 3:
            ce0, pe0 = self._iv_buf[0]
            ce1, pe1 = self._iv_buf[1]
            ce2, pe2 = self._iv_buf[2]
            if ce0 and ce1 and ce2 and float(ce0) > 0 and float(ce1) > 0 and float(ce2) > 0:
                if float(ce1) > float(ce0) * 1.05 and float(ce2) < float(ce1) * 0.97:
                    score -= 2.0
                    fired.append("ce_iv_fading")

        # ── Live depth signals (15–18) — only fire when depth feed is active ──────
        # These use the real-time order book rather than price/IV proxies.
        # In replay/offline mode self._current_depth_ctx is None → signals silent.
        _d = self._current_depth_ctx
        if _d is not None:
            # 15. CE bid dominance: CE buyers pressing — calls being chased up.
            if _d.ce_valid:
                ce_bid = float(_d.ce.bid_qty or 0)
                ce_ask = float(_d.ce.ask_qty or 0)
                if ce_ask > 0 and ce_bid > ce_ask * 1.5:
                    score += 1.5
                    fired.append("depth_ce_bid_dom")
                # 18. CE ask dominance: calls being dumped — bearish.
                elif ce_bid > 0 and ce_ask > ce_bid * 2.0:
                    score -= 1.5
                    fired.append("depth_ce_ask_dom")

            # 16. PE bid dominance: PE buyers pressing — puts being chased up (bearish).
            if _d.pe_valid:
                pe_bid = float(_d.pe.bid_qty or 0)
                pe_ask = float(_d.pe.ask_qty or 0)
                if pe_ask > 0 and pe_bid > pe_ask * 1.5:
                    score -= 1.5
                    fired.append("depth_pe_bid_dom")
                # 17. PE ask dominance: puts being offered into — sellers absorbing (bullish).
                elif pe_bid > 0 and pe_ask > pe_bid * 2.0:
                    score += 1.5
                    fired.append("depth_pe_offer_dom")

        basis = ",".join(fired) if fired else "no_signals"
        if score > 0:
            return Direction.CE, f"multi_signal_ce(score={score:.1f}:{basis})", score
        if score < 0:
            return Direction.PE, f"multi_signal_pe(score={score:.1f}:{basis})", score

        # Exact tie: fall back to 15m then 5m momentum, then default PE.
        # PE is the conservative default — Indian equity IV skew structurally favours
        # put premium, so an ambiguous market is more likely sideways-to-bearish.
        if r15 is not None and float(r15) != 0.0:
            return (Direction.CE if float(r15) > 0 else Direction.PE), f"tie_r15m({basis})", 0.0
        if r5 is not None and float(r5) != 0.0:
            return (Direction.CE if float(r5) > 0 else Direction.PE), f"tie_r5m({basis})", 0.0
        return Direction.PE, f"default_pe({basis})", 0.0

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

    def _trend_fade_block(
        self,
        votes: list[StrategyVote],
        snap: SnapshotAccessor,
    ) -> Optional[str]:
        """#3 Trend-fade guard — don't fade the dominant VWAP trend on a shallow pullback.

        A counter-trend option (a PE while price still holds above VWAP, or a CE while
        price holds below VWAP) is blocked ONLY while the opposing move is still a shallow
        pullback — i.e. the 30m return has not yet become a genuine trend the other way.
        Once the reversal is real (|fut_return_30m| beyond the strong threshold) the guard
        releases, so true reversals remain tradeable. Default-OFF via TREND_FADE_GUARD_ENABLED;
        when off this returns None and the gate is a no-op.

        Returns "trend_fade_guard:PE" / "trend_fade_guard:CE", or None.
        """
        if not as_bool(os.getenv("TREND_FADE_GUARD_ENABLED", "false")):
            return None
        pvw = snap.price_vs_vwap
        r30m = snap.fut_return_30m
        if pvw is None or r30m is None:
            return None
        vwap_min = float(os.getenv("TREND_FADE_GUARD_VWAP_MIN", "0.001"))
        r30m_strong = float(os.getenv("TREND_FADE_GUARD_R30M_STRONG", "0.005"))
        entry_dirs = {
            str(v.direction.value if hasattr(v.direction, "value") else v.direction or "").upper()
            for v in votes
            if v.signal_type == SignalType.ENTRY and v.direction in (Direction.CE, Direction.PE)
        }
        # Bullish day structure (price above VWAP) + only a shallow dip → don't buy puts.
        if "PE" in entry_dirs and pvw >= vwap_min and r30m > -r30m_strong:
            return "trend_fade_guard:PE"
        # Bearish day structure (price below VWAP) + only a shallow bounce → don't buy calls.
        if "CE" in entry_dirs and pvw <= -vwap_min and r30m < r30m_strong:
            return "trend_fade_guard:CE"
        return None

    def _derive_entry_blocker(
        self,
        *,
        votes: list[StrategyVote],
        snap: SnapshotAccessor,
        regime_signal: RegimeSignal,
    ) -> str:
        # Mirror the env-driven early returns in _process_entry_votes so the
        # trace records a real reason instead of falling through to a generic
        # policy_checks/no_selection block.
        if not is_in_configured_time_window(snap):
            return "entry_time_windows"
        tagger = os.getenv("ENTRY_REGIME_TAGGER", "").strip()
        if tagger and not is_session_regime_allowed(self._session_regime_tag):
            return "entry_regime_tag"
        skip_brain = (
            self._strategy_profile_id in _PROFILES_ML_ENTRY_DET_DIRECTION
            and as_bool(os.getenv("ML_ENTRY_DET_SKIP_BRAIN_GATE", "false"))
        )
        if not skip_brain:
            entry_votes_for_brain = [
                v for v in votes
                if v.signal_type == SignalType.ENTRY and v.direction in (Direction.CE, Direction.PE)
            ]
            brain_decision = self._brain.gate_entry(entry_votes_for_brain, self._day_context)
            if not brain_decision.allowed:
                return f"brain_gate:{brain_decision.reason}"

        # ── Trader discipline gates (order MUST mirror _process_entry_votes) ───
        # NOTE: this method is the single place that derives the trace's blocker
        # string. Every early-return in _process_entry_votes must have a matching
        # branch here in the SAME ORDER, or the trace will show the wrong gate.

        # 0. Minimum re-entry spacing: never enter immediately after any exit.
        reentry_gap = int(os.getenv("MIN_REENTRY_BARS", "3"))
        if self._last_any_exit_bar is not None:
            bars_since_exit = self._session_event_count - self._last_any_exit_bar
            if bars_since_exit < reentry_gap:
                return f"min_reentry_gap:{bars_since_exit}<{reentry_gap}"

        # 1. SIDEWAYS + returns_mixed: market has no intraday conviction.
        #    A trader never enters when returns are contradicting themselves.
        if (
            regime_signal.regime is not None
            and str(regime_signal.regime.value if hasattr(regime_signal.regime, "value") else regime_signal.regime).upper() == "SIDEWAYS"
            and "returns_mixed" in (regime_signal.reason or "")
        ):
            cooldown_bars = int(os.getenv("SIDEWAYS_MIXED_COOLDOWN_BARS", "0"))
            if cooldown_bars == 0:
                return "sideways_returns_mixed"

        # 2. Post-STOP_LOSS cooldown: after a stop, wait N bars before re-entering.
        stop_cooldown = int(os.getenv("STOP_LOSS_COOLDOWN_BARS", "5"))
        if self._last_stop_bar is not None:
            bars_since_stop = self._session_event_count - self._last_stop_bar
            if bars_since_stop < stop_cooldown:
                return f"stop_loss_cooldown:{bars_since_stop}<{stop_cooldown}"

        # 3. Direction-flip block: after a STOP_LOSS, don't flip direction for N bars.
        flip_cooldown = int(os.getenv("DIRECTION_FLIP_COOLDOWN_BARS", "8"))
        _entry_v = [v for v in votes if v.signal_type == SignalType.ENTRY
                    and v.direction in (Direction.CE, Direction.PE)]
        _cur_dirs = {str(v.direction.value if hasattr(v.direction, "value") else v.direction).upper()
                     for v in _entry_v}
        if self._last_stop_bar is not None and self._last_exit_direction:
            bars_since_stop = self._session_event_count - self._last_stop_bar
            if bars_since_stop < flip_cooldown and _entry_v and self._last_exit_direction not in _cur_dirs:
                return f"direction_flip_cooldown:{bars_since_stop}<{flip_cooldown}"

        # 4. Zero-MFE same-direction block: last trade had no favorable excursion.
        zero_mfe_cool = int(os.getenv("ZERO_MFE_COOLDOWN_BARS", "10"))
        if self._last_zero_mfe_bar is not None and self._last_zero_mfe_direction:
            bars_since_zero = self._session_event_count - self._last_zero_mfe_bar
            if bars_since_zero < zero_mfe_cool and self._last_zero_mfe_direction in _cur_dirs:
                return f"zero_mfe_cooldown:{bars_since_zero}<{zero_mfe_cool}"

        # 5. Direction-evidence agreement: don't trade against bull/bear evidence.
        ev = getattr(regime_signal, "evidence", None) or {}
        try:
            _bull = float(ev.get("bull_score", -1)); _bear = float(ev.get("bear_score", -1))
        except (TypeError, ValueError):
            _bull = _bear = -1.0
        if _bull >= 0 and _bear >= 0 and _cur_dirs:
            ev_support = float(os.getenv("DIRECTION_EVIDENCE_SUPPORT_MIN", "0.2"))
            ev_oppose = float(os.getenv("DIRECTION_EVIDENCE_OPPOSING_MAX", "0.6"))
            if "PE" in _cur_dirs and _bull > ev_oppose and _bear < ev_support:
                return "direction_evidence_mismatch:PE"
            if "CE" in _cur_dirs and _bear > ev_oppose and _bull < ev_support:
                return "direction_evidence_mismatch:CE"

        # 6. Trend-fade guard (#3): mirror of _process_entry_votes gate 6.
        _fade = self._trend_fade_block(votes, snap)
        if _fade is not None:
            return _fade
        # ── End discipline gates ───────────────────────────────────────────────

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
        if self._strategy_profile_id in _PROFILES_ML_ENTRY_CONSENSUS:
            ml_votes = [v for v in entry_votes if v.strategy_name == "ML_ENTRY"]
            if not ml_votes:
                return "ml_timing_gate"
            ml_vote = max(ml_votes, key=lambda v: float(v.confidence or 0))
            shadow_dir, _, shadow_score = self._shadow_direction_from_snapshot(snap)
            hint_dir, ce_prob = extract_ml_direction_hint(ml_vote)
            rule_votes = [
                v
                for v in entry_votes
                if v.strategy_name != "ML_ENTRY" and v.direction in (Direction.CE, Direction.PE)
            ]
            consensus = resolve_direction_consensus(
                snap=snap,
                rule_votes=rule_votes,
                shadow_direction=shadow_dir,
                shadow_score=shadow_score,
                ml_direction_hint=hint_dir,
                ml_ce_prob=ce_prob,
                regime_signal=regime_signal,
            )
            if consensus.vetoed or consensus.direction is None:
                return f"direction_consensus:{consensus.veto_reason}"
        if ce_votes and pe_votes and not self._entry_policy_can_resolve_direction_conflict():
            return "direction_conflict"
        if (
            self._strategy_profile_id not in _PROFILES_RELAX_REGIME_CONF
            and regime_signal.confidence < 0.60
        ):
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
        def _f(x):
            try: return float(x)
            except (TypeError, ValueError): return 0.0
        _ev0 = getattr(regime_signal, "evidence", None) or {}
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
        # Always emit the direction-evidence row (bull/bear scores) so the UI can show
        # whether the trade agreed with the market evidence — even on passed trades.
        if "bull_score" in _ev0 or "bear_score" in _ev0:
            gates.append(
                {
                    "gate_id": "direction_evidence",
                    "gate_group": "evidence",
                    "status": "pass",
                    "reason_code": None,
                    "message": f"bull={_f(_ev0.get('bull_score')):.2f} bear={_f(_ev0.get('bear_score')):.2f}",
                    "metrics": {
                        "bull_score": _f(_ev0.get("bull_score")),
                        "bear_score": _f(_ev0.get("bear_score")),
                        "r5m": _f(_ev0.get("r5m")),
                        "r15m": _f(_ev0.get("r15m")),
                    },
                }
            )
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
        if blocker == "entry_time_windows":
            gates.append(
                {
                    "gate_id": "entry_time_windows",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "outside_configured_time_window",
                    "message": f"snapshot outside ENTRY_TIME_WINDOWS={os.getenv('ENTRY_TIME_WINDOWS','')}",
                    "metrics": {},
                }
            )
            return gates, "blocked", "entry_time_windows", False
        if blocker == "entry_regime_tag":
            gates.append(
                {
                    "gate_id": "entry_regime_tag",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "regime_tag_not_allowed",
                    "message": f"session regime tag={self._session_regime_tag} not in ENTRY_REGIME_ALLOWED_TAGS",
                    "metrics": {},
                }
            )
            return gates, "blocked", "entry_regime_tag", False
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
        if blocker is not None and blocker.startswith("min_reentry_gap:"):
            bars_info = blocker.split(":", 1)[1]
            gates.append(
                {
                    "gate_id": "min_reentry_gap",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "min_reentry_gap",
                    "message": f"Minimum re-entry spacing: {bars_info} bars since last exit",
                    "metrics": {"bars_since_exit": int(bars_info.split("<")[0])},
                }
            )
            return gates, "blocked", "min_reentry_gap", False
        if blocker == "sideways_returns_mixed":
            gates.append(
                {
                    "gate_id": "sideways_returns_mixed",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "sideways_returns_mixed",
                    "message": "SIDEWAYS + returns_mixed: no directional conviction — trader discipline block",
                    "metrics": {"regime_confidence": regime_signal.confidence},
                }
            )
            return gates, "blocked", "sideways_returns_mixed", False
        if blocker is not None and blocker.startswith("stop_loss_cooldown:"):
            bars_info = blocker.split(":", 1)[1]
            gates.append(
                {
                    "gate_id": "stop_loss_cooldown",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "stop_loss_cooldown",
                    "message": f"Cooldown after STOP_LOSS: {bars_info} bars — no re-entry yet",
                    "metrics": {"bars_since_stop": int(bars_info.split("<")[0])},
                }
            )
            return gates, "blocked", "stop_loss_cooldown", False
        if blocker is not None and blocker.startswith("direction_flip_cooldown:"):
            bars_info = blocker.split(":", 1)[1]
            gates.append(
                {
                    "gate_id": "direction_flip_cooldown",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "direction_flip_cooldown",
                    "message": f"Direction flip blocked within cooldown: {bars_info} bars — wait for market to settle",
                    "metrics": {"bars_since_stop": int(bars_info.split("<")[0]),
                                "last_direction": self._last_exit_direction or ""},
                }
            )
            return gates, "blocked", "direction_flip_cooldown", False
        if blocker is not None and blocker.startswith("zero_mfe_cooldown:"):
            bars_info = blocker.split(":", 1)[1]
            gates.append(
                {
                    "gate_id": "zero_mfe_cooldown",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "zero_mfe_cooldown",
                    "message": f"Last {self._last_zero_mfe_direction or ''} trade had zero favorable excursion — same-direction blocked for {bars_info} bars",
                    "metrics": {"bars_since_zero_mfe": int(bars_info.split("<")[0]),
                                "last_direction": self._last_zero_mfe_direction or ""},
                }
            )
            return gates, "blocked", "zero_mfe_cooldown", False
        if blocker is not None and blocker.startswith("direction_evidence_mismatch:"):
            blocked_dir = blocker.split(":", 1)[1]
            _ev = getattr(regime_signal, "evidence", None) or {}
            def _f(x):
                try: return float(x)
                except (TypeError, ValueError): return 0.0
            gates.append(
                {
                    "gate_id": "direction_evidence",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "direction_evidence_mismatch",
                    "message": f"{blocked_dir} entry against market evidence — bull/bear scores oppose the trade direction",
                    "metrics": {
                        "bull_score": _f(_ev.get("bull_score")),
                        "bear_score": _f(_ev.get("bear_score")),
                        "r5m": _f(_ev.get("r5m")),
                        "r15m": _f(_ev.get("r15m")),
                    },
                }
            )
            return gates, "blocked", "direction_evidence", False
        if blocker is not None and blocker.startswith("trend_fade_guard:"):
            blocked_dir = blocker.split(":", 1)[1]
            def _ff(x):
                try: return float(x)
                except (TypeError, ValueError): return 0.0
            gates.append(
                {
                    "gate_id": "trend_fade_guard",
                    "gate_group": "policy",
                    "status": "blocked",
                    "reason_code": "trend_fade_guard",
                    "message": f"{blocked_dir} entry fades the dominant VWAP trend on a shallow pullback — trader discipline block",
                    "metrics": {
                        "price_vs_vwap": _ff(getattr(snap, "price_vs_vwap", None)),
                        "fut_return_30m": _ff(getattr(snap, "fut_return_30m", None)),
                    },
                }
            )
            return gates, "blocked", "trend_fade_guard", False
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
        if raw_signals.get("direction_source") == "direction_consensus":
            dir_sources = raw_signals.get("direction_consensus_sources") or {}
            ce = raw_signals.get("direction_consensus_ce", 0)
            pe = raw_signals.get("direction_consensus_pe", 0)
            margin = raw_signals.get("direction_consensus_margin", 0)
            winner = vote.direction.value if vote.direction else "?"
            gates.append(
                {
                    "gate_id": "direction_consensus",
                    "gate_group": "direction",
                    "status": "pass",
                    "reason_code": None,
                    "message": f"{winner}  ce={ce:.2f} pe={pe:.2f} margin={margin:.2f}",
                    "metrics": {
                        "ce_score": ce,
                        "pe_score": pe,
                        "margin": margin,
                        "shadow_basis": raw_signals.get("direction_consensus_shadow_basis"),
                        **{k: round(v, 3) for k, v in dir_sources.items()},
                    },
                }
            )
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
        policy_mode = str(raw_signals.get("_entry_policy_mode") or "").strip().lower()
        policy_allowed = raw_signals.get("_policy_allowed")
        # Bypass mode: the vote declared _entry_policy_mode=bypass — treat as pass regardless
        # of whether _policy_allowed was mirrored. This prevents the trace showing "blocked"
        # when the entry fired correctly via the consensus bypass path.
        if policy_mode == "bypass" and policy_allowed is None:
            policy_allowed = True
        policy_reason = str(raw_signals.get("_policy_reason") or "").strip()
        policy_checks = raw_signals.get("_policy_checks") or {}
        gates.append(
            {
                "gate_id": "policy_checks",
                "gate_group": "policy",
                "status": ("pass" if policy_allowed is True else "blocked"),
                "reason_code": (vote.decision_reason_code if policy_allowed is not True else "policy_allowed"),
                "message": policy_reason or (f"bypass:entry_prob={raw_signals.get('entry_prob','?')}>={raw_signals.get('entry_threshold','?')}" if policy_mode == "bypass" else None),
                "metrics": {
                    "policy_mode": policy_mode or None,
                    "policy_score": raw_signals.get("_policy_score"),
                    "entry_prob": raw_signals.get("entry_prob"),
                    "entry_threshold": raw_signals.get("entry_threshold"),
                    **{k: v for k, v in policy_checks.items() if k not in ("mode", "strategy")},
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

    @staticmethod
    def _summary_votes_digest(votes: list[StrategyVote]) -> list[dict[str, Any]]:
        """Compact per-vote rows for the decision summary (entry/AVOID only).

        Sorted by confidence desc so the most relevant candidate is first. Only
        the fields needed to answer "which strategies wanted in, how strongly,
        and what grade/tier they earned" — not the full raw_signals blob.
        """
        rows: list[dict[str, Any]] = []
        relevant = [
            v for v in votes
            if v.signal_type == SignalType.ENTRY or v.direction == Direction.AVOID
        ]
        for vote in sorted(relevant, key=lambda v: float(v.confidence or 0.0), reverse=True):
            raw = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
            rows.append({
                "strategy": str(vote.strategy_name or "").strip() or None,
                "direction": (vote.direction.value if vote.direction is not None else None),
                "confidence": _safe_float(vote.confidence),
                "grade": str(raw.get("entry_grade") or "").strip() or None,
                "tier": str(raw.get("tier") or "").strip() or None,
            })
        return rows

    def _emit_decision_summary(
        self,
        *,
        snap: SnapshotAccessor,
        action: str,
        regime_signal: Optional[RegimeSignal],
        votes: list[StrategyVote],
        signal: Optional[TradeSignal],
        position: Optional[PositionContext],
        blocking_gate: Optional[str] = None,
        warmup_blocked: bool = False,
    ) -> None:
        """Append one always-on line per evaluate() call to decisions.jsonl.

        This is the single human-/grep-readable summary of "what happened at
        minute T": the input (price/regime/phase), the engine state, the votes
        that wanted in, and the output (trade taken, or the gate that blocked
        it). It closes observability gap #1 in docs/OBSERVABILITY_GUIDE.md and
        is the canonical place to look first — the env-gated decision_trace is
        the deep layer beneath it.

        Never raises: observability must not break the trading loop.
        """
        try:
            risk_ctx = getattr(self._risk, "context", None)
            regime_val = None
            regime_conf = None
            if regime_signal is not None:
                _r = getattr(regime_signal, "regime", None)
                regime_val = str(getattr(_r, "value", _r) or "").strip() or None
                regime_conf = _safe_float(getattr(regime_signal, "confidence", None))

            record: dict[str, Any] = {
                "snapshot_id": snap.snapshot_id,
                "ts": isoformat_ist(snap.timestamp_or_now),
                "trade_date_ist": isoformat_ist(snap.timestamp_or_now)[:10],
                "engine_mode": self._engine_mode,
                "action": action,
                "blocking_gate": str(blocking_gate or "").strip() or None,
                "input": {
                    "session_phase": snap.session_phase or None,
                    "fut_close": _safe_float(snap.fut_close),
                    "atm_strike": snap.atm_strike,
                    "or_width": _safe_float(snap.or_width),
                    "regime": regime_val,
                    "regime_conf": regime_conf,
                },
                "engine_state": {
                    "has_position": position is not None,
                    "is_halted": bool(getattr(self._risk, "is_halted", False)),
                    "is_paused": bool(getattr(self._risk, "is_paused", False)),
                    "warmup_blocked": bool(warmup_blocked),
                    "session_pnl_total": _safe_float(getattr(risk_ctx, "session_pnl_total", None)),
                    "session_trade_count": int(getattr(risk_ctx, "session_trade_count", 0) or 0),
                    "consecutive_losses": int(getattr(risk_ctx, "consecutive_losses", 0) or 0),
                    "bars_evaluated": int(self._session_event_count),
                },
                "votes": self._summary_votes_digest(votes),
            }

            if position is not None:
                record["position"] = {
                    "position_id": str(getattr(position, "position_id", None) or "").strip() or None,
                    "direction": str(getattr(position, "direction", None) or "").strip() or None,
                    "strike": getattr(position, "strike", None),
                    "bars_held": int(getattr(position, "bars_held", 0) or 0),
                    "pnl_pct": _safe_float(getattr(position, "pnl_pct", None)),
                }

            if signal is not None:
                out_raw: dict[str, Any] = {}
                if isinstance(signal.votes, list) and signal.votes:
                    _wraw = signal.votes[0].raw_signals
                    out_raw = _wraw if isinstance(_wraw, dict) else {}
                def _ev(x: Any) -> Optional[str]:
                    if x is None:
                        return None
                    return str(getattr(x, "value", x)).strip() or None
                record["output"] = {
                    "signal_type": _ev(signal.signal_type),
                    "direction": _ev(signal.direction),
                    "strike": getattr(signal, "strike", None),
                    "exit_reason": _ev(getattr(signal, "exit_reason", None)),
                    "reason": str(getattr(signal, "reason", None) or "").strip() or None,
                    "grade": str(out_raw.get("entry_grade") or "").strip() or None,
                    "tier": str(out_raw.get("tier") or "").strip() or None,
                    "execution_path": str(out_raw.get("_execution_path") or "").strip() or None,
                }

            self._log.log_decision_summary(record)
        except Exception:
            logger.exception("failed to log decision summary (snapshot=%s)", getattr(snap, "snapshot_id", "?"))

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
        shadow_dir, shadow_full_basis, shadow_score = self._shadow_direction_from_snapshot(snap)
        # Derive the execution_path from the winning vote's annotation so the trace top-level
        # immediately answers "HOW did this fire?" without reading candidates[0].ordered_gates.
        execution_path: str | None = None
        if signal is not None:
            for vote in votes:
                rs = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
                ep = str(rs.get("_execution_path") or "").strip()
                if ep:
                    execution_path = ep
                    break
            if execution_path is None:
                execution_path = "direct_candidate"
        trace = builder.finalize(
            final_outcome=final_outcome,
            primary_blocker_gate=("warmup" if warmup_blocked else blocker),
            summary_metrics={
                "vote_count": len(votes),
                "entry_vote_count": len([vote for vote in votes if vote.signal_type == SignalType.ENTRY]),
                "shadow_score": round(shadow_score, 2),
                "shadow_dir": shadow_dir.value,
                "shadow_basis": shadow_full_basis,
            },
        )
        if execution_path is not None:
            trace["execution_path"] = execution_path
        # Attach brain context to trace so the UI blocker funnel can show
        # day_score and consensus details without a separate API call.
        if self._day_context is not None:
            trace["brain"] = {
                "day_score": self._day_context.day_score.value,
                "day_score_confidence": round(self._day_context.day_score_confidence, 4),
                "day_score_reason": self._day_context.day_score_reason,
                "size_multiplier": round(self._day_context.size_multiplier, 4),
                "carry_consecutive_losses": self._day_context.session_carry.consecutive_losses_at_close,
            }
        return trace

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

    def _write_brain_state(self, trade_date: date) -> None:
        """Write brain morning context to brain_state.json for dashboard observability."""
        if self._day_context is None:
            return
        try:
            paths = resolve_runtime_artifact_paths()
            self._brain_state_path = paths.root / "brain_state.json"
            payload = {
                "trade_date": trade_date.isoformat(),
                "brain_enabled": self._brain.enabled,
                "day_context": self._day_context.to_dict(),
            }
            tmp = self._brain_state_path.with_name("brain_state.json.tmp")
            self._brain_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._brain_state_path)
        except Exception as exc:
            logger.warning("brain_state write failed error=%s", exc)

    def _mark_strike_veto(self, vote: StrategyVote, mode: str, reason: str) -> None:
        """Flag a vote as vetoed by the strike layer so every entry path skips it."""
        vote.raw_signals["_strike_vetoed"] = True
        vote.raw_signals["_strike_veto_mode"] = str(mode)
        vote.raw_signals["_strike_veto_reason"] = str(reason)
        logger.info(
            "strike_veto strategy=%s dir=%s mode=%s reason=%s → no_trade",
            vote.strategy_name,
            getattr(vote.direction, "value", vote.direction),
            mode, reason,
        )

    def _apply_strike_selection(self, vote: StrategyVote, snap: SnapshotAccessor, regime: str = "") -> None:
        if bool(vote.raw_signals.get("_lock_strike_selection")):
            return
        if self._run_risk_config.atm_strike_only and not self._allow_non_atm_for_ml_entry(vote):
            vote = self._force_atm_strike(vote, snap)
            return

        if (
            os.getenv("STRATEGY_SMART_STRIKE_ENABLED", "").strip() == "1"
            and self._allow_non_atm_for_ml_entry(vote)
        ):
            from ..signals.option_selector import select_strike as _smart_select

            class _Proxy:
                def __init__(self, v: StrategyVote) -> None:
                    conf = float(v.confidence or 0.0)
                    self.ce_prob = conf if v.direction == Direction.CE else 0.0
                    self.pe_prob = conf if v.direction == Direction.PE else 0.0

            direction_str = vote.direction.value
            selection = _smart_select(snap, direction_str, _Proxy(vote), regime=regime)
            logger.info(
                "strike_selection strategy=%s dir=%s atm=%s regime=%s → strike=%s mode=%s reason=%s",
                vote.strategy_name, direction_str, snap.atm_strike, regime,
                selection.strike, selection.mode, selection.reason,
            )
            if selection.strike is not None and int(selection.strike) > 0:
                ltp = snap.option_ltp(direction_str, int(selection.strike))
                if ltp is not None and float(ltp) > 0:
                    vote.proposed_strike = int(selection.strike)
                    vote.proposed_entry_premium = float(ltp)
                    vote.raw_signals["_strike_policy"] = f"smart_strike_{selection.mode}"
                    vote.raw_signals["_strike_selected"] = int(selection.strike)
                    vote.raw_signals["_strike_selected_premium"] = float(round(ltp, 4))
                    vote.raw_signals["_strike_mode"] = selection.mode
                    vote.raw_signals["_strike_reason"] = selection.reason
            elif str(selection.mode or "").startswith("rejected"):
                # Strike layer vetoed (premium hard-cap, IV too high, …). This is a
                # real no-trade — record it so every entry path skips the vote
                # instead of silently falling back to a default strike.
                self._mark_strike_veto(vote, selection.mode, selection.reason)
            return

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



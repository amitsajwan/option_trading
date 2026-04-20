"""Pure-ML runtime engine (staged runtime bundle only)."""

from __future__ import annotations

import logging
import os
import time as wall_time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from contracts_app import isoformat_ist

from ..contracts import ExitReason, PositionContext, SignalType, SnapshotPayload, StrategyEngine, TradeSignal
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
from ..risk.manager import RiskManager
from .runtime_artifacts import RuntimeArtifactStore, build_runtime_state_payload
from .decision_annotation import annotate_signal_contract as apply_signal_contract
from .pure_ml_staged_runtime import PureMLRuntimeControls, StagedRuntimeDecision, load_staged_model_package, load_staged_policy, predict_staged
from .rolling_feature_state import RollingFeatureState
from .regime import RegimeClassifier
from .snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


class PureMLEngine(StrategyEngine):
    """Pure-ML entry runtime with hard operational gates and staged inference."""

    def __init__(
        self,
        *,
        model_package_path: str,
        threshold_report_path: str,
        max_feature_age_sec: int = 90,
        max_nan_features: int = 3,
        max_hold_bars: int = 15,
        min_oi: float = 50000.0,
        min_volume: float = 15000.0,
        min_edge: Optional[float] = None,
        stop_loss_pct: float = 0.05,
        target_pct: float = 0.20,
        signal_logger: Optional[SignalLogger] = None,
        runtime_artifact_dir: Optional[Path | str] = None,
        strategy_profile_id: Optional[str] = None,
    ) -> None:
        self._model_package = load_staged_model_package(model_package_path)
        self._staged_runtime_policy: dict[str, Any] = load_staged_policy(threshold_report_path)
        runtime_payload = dict(self._staged_runtime_policy.get("runtime") or {})
        bundle_runtime_payload = dict(self._model_package.get("runtime") or {})
        bypass_deterministic_gates = _env_bool(
            "STRATEGY_ML_PURE_BYPASS_GATES",
            bool(runtime_payload.get("bypass_deterministic_gates", bundle_runtime_payload.get("bypass_deterministic_gates", False))),
        )
        self._runtime_controls = PureMLRuntimeControls(
            block_expiry=bool(runtime_payload.get("block_expiry", bundle_runtime_payload.get("block_expiry", False))),
            bypass_deterministic_gates=bypass_deterministic_gates,
        )
        self._regime = RegimeClassifier()
        self._feature_state = RollingFeatureState()
        self._tracker = PositionTracker()
        self._risk = RiskManager()
        self._log = signal_logger or SignalLogger()
        self._runtime_artifacts = RuntimeArtifactStore(runtime_artifact_dir)
        self._max_feature_age_sec = max(0, int(max_feature_age_sec))
        self._max_nan_features = max(0, int(max_nan_features))
        self._max_hold_bars = max(1, int(max_hold_bars))
        self._min_oi = max(0.0, float(min_oi))
        self._min_volume = max(0.0, float(min_volume))
        self._stop_loss_pct = max(0.0, float(stop_loss_pct))
        self._target_pct = max(0.0, float(target_pct))
        if min_edge is not None:
            logger.warning(
                "pure ml staged engine ignores constructor min_edge=%.4f; using staged runtime policy selected_min_edge",
                float(min_edge),
            )
        self._startup_warmup_minutes = max(0.0, float(os.getenv("STRATEGY_STARTUP_WARMUP_MINUTES", "0") or 0.0))
        self._startup_warmup_events = max(0, int(os.getenv("STRATEGY_STARTUP_WARMUP_EVENTS", "0") or 0))
        self._session_start_monotonic: Optional[float] = None
        self._session_event_count = 0
        self._bars_evaluated = 0
        self._entry_count = 0
        self._last_entry_at: Optional[str] = None
        self._last_event: Optional[dict[str, Any]] = None
        self._last_decision: Optional[dict[str, Any]] = None
        self._session_trade_date: Optional[str] = None
        self._session_started_at_ist: Optional[str] = None
        self._session_updated_at_ist: Optional[str] = None
        self._hold_counts: dict[str, int] = {}
        self._run_id: Optional[str] = None
        self._model_run_id: Optional[str] = None
        self._model_group: Optional[str] = None
        self._engine_mode = "ml_pure"
        self._strategy_family_version = "ML_PURE_STAGED_V1"
        default_profile_id = "ml_pure_staged_v1"
        self._strategy_profile_id = str(strategy_profile_id or default_profile_id).strip() or default_profile_id
        self._set_logger_context(None)
        logger.info(
            "pure ml staged engine initialized max_feature_age_sec=%d max_nan_features=%d min_oi=%.0f min_volume=%.0f block_expiry=%s bypass_deterministic_gates=%s",
            self._max_feature_age_sec,
            self._max_nan_features,
            self._min_oi,
            self._min_volume,
            str(self._runtime_controls.block_expiry).lower(),
            str(self._runtime_controls.bypass_deterministic_gates).lower(),
        )
        self._write_runtime_state(last_event={"event": "engine_init"})

    def set_run_context(self, run_id: Optional[str], metadata: Optional[dict[str, Any]] = None) -> None:
        new_run_id = str(run_id or "").strip() or None
        if new_run_id and new_run_id != self._run_id:
            self._feature_state.reset()
        self._run_id = new_run_id
        if isinstance(metadata, dict):
            profile = str(metadata.get("strategy_profile_id") or "").strip()
            if profile:
                self._strategy_profile_id = profile
            model_run_id = str(metadata.get("model_run_id") or metadata.get("ml_pure_run_id") or "").strip()
            if model_run_id:
                self._model_run_id = model_run_id
            model_group = str(metadata.get("model_group") or "").strip()
            if model_group:
                self._model_group = model_group
            regime_payload = metadata.get("regime_config")
            if isinstance(regime_payload, dict):
                self._regime.configure(regime_payload)
        self._set_logger_context(run_id)
        self._write_runtime_state(
            last_event={
                "event": "run_context",
                "run_id": self._run_id,
                "model_run_id": self._model_run_id,
                "model_group": self._model_group,
                "strategy_profile_id": self._strategy_profile_id,
            }
        )

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

    def on_session_start(self, trade_date: date) -> None:
        self._session_start_monotonic = wall_time.monotonic()
        self._session_event_count = 0
        self._bars_evaluated = 0
        self._entry_count = 0
        self._last_entry_at = None
        self._last_event = {"event": "session_start", "trade_date": trade_date.isoformat()}
        self._last_decision = None
        self._session_trade_date = trade_date.isoformat()
        self._session_started_at_ist = isoformat_ist(datetime.now(timezone.utc))
        self._session_updated_at_ist = self._session_started_at_ist
        self._hold_counts = {}
        self._feature_state.on_session_start(trade_date)
        self._tracker.on_session_start(trade_date)
        self._risk.on_session_start(trade_date)
        self._write_runtime_state()
        self._append_runtime_metric(
            {
                "event": "session_start",
                "trade_date": trade_date.isoformat(),
                "bar": self._bars_evaluated,
            }
        )
        logger.info("pure ml engine session started: %s", trade_date.isoformat())

    def on_session_end(self, trade_date: date) -> None:
        if self._tracker.has_position:
            position = self._tracker.current_position
            exit_signal = self._tracker.force_exit(_session_end_snapshot(trade_date), ExitReason.TIME_STOP)
            if exit_signal is not None and position is not None:
                self._annotate_signal_contract(
                    exit_signal,
                    decision_reason_code="time_stop",
                    decision_metrics={"confidence": float(exit_signal.confidence or 0.0)},
                )
                self._log.log_signal(exit_signal, acted_on=True)
                self._handle_position_closed(exit_signal, position)
                self._last_event = {
                    "event": "session_end_exit",
                    "trade_date": trade_date.isoformat(),
                    "exit_reason": exit_signal.exit_reason.value if exit_signal.exit_reason else None,
                    "position_id": exit_signal.position_id,
                }
                self._last_decision = {
                    "event": "exit",
                    "reason": exit_signal.exit_reason.value if exit_signal.exit_reason else None,
                    "signal_id": exit_signal.signal_id,
                }
                self._session_updated_at_ist = isoformat_ist(datetime.now(timezone.utc))
                self._write_runtime_state()
                self._append_runtime_metric(
                    {
                        "event": "exit",
                        "ts": exit_signal.timestamp.isoformat(),
                        "bar": self._bars_evaluated,
                        "snapshot_id": exit_signal.snapshot_id,
                        "signal_id": exit_signal.signal_id,
                        "position_id": exit_signal.position_id,
                        "reason": exit_signal.exit_reason.value if exit_signal.exit_reason else None,
                    }
                )
        self._feature_state.on_session_end()
        self._risk.on_session_end(trade_date)
        self._session_start_monotonic = None
        self._last_event = {"event": "session_end", "trade_date": trade_date.isoformat()}
        self._session_updated_at_ist = isoformat_ist(datetime.now(timezone.utc))
        self._write_runtime_state()
        self._append_runtime_metric(
            {
                "event": "session_end",
                "trade_date": trade_date.isoformat(),
                "bar": self._bars_evaluated,
            }
        )
        logger.info("pure ml engine session ended: %s", trade_date.isoformat())

    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        self._session_event_count += 1
        self._bars_evaluated += 1
        snap = SnapshotAccessor(snapshot)
        rolling_features = self._feature_state.update(snap)
        position = self._tracker.current_position
        risk = self._risk.context
        self._risk.update(snap, position)

        if position is not None:
            system_exit = self._tracker.update(snap, risk)
            if system_exit is not None:
                self._annotate_signal_contract(
                    system_exit,
                    decision_reason_code=(str(system_exit.exit_reason.value).lower() if system_exit.exit_reason else None),
                    decision_metrics={"confidence": float(system_exit.confidence or 0.0)},
                )
                self._log.log_signal(system_exit, acted_on=True)
                self._handle_position_closed(system_exit, position)
                self._last_event = {
                    "event": "exit",
                    "snapshot_id": snap.snapshot_id,
                    "signal_id": system_exit.signal_id,
                    "position_id": system_exit.position_id,
                    "reason": system_exit.exit_reason.value if system_exit.exit_reason else None,
                }
                self._last_decision = {
                    "event": "exit",
                    "reason": system_exit.exit_reason.value if system_exit.exit_reason else None,
                    "confidence": float(system_exit.confidence or 0.0),
                }
                self._session_updated_at_ist = isoformat_ist(snap.timestamp_or_now)
                self._write_runtime_state()
                self._append_runtime_metric(
                    {
                        "event": "exit",
                        "ts": isoformat_ist(snap.timestamp_or_now),
                        "bar": self._bars_evaluated,
                        "snapshot_id": snap.snapshot_id,
                        "signal_id": system_exit.signal_id,
                        "position_id": system_exit.position_id,
                        "direction": system_exit.direction,
                        "strike": system_exit.strike,
                        "reason": system_exit.exit_reason.value if system_exit.exit_reason else None,
                        "confidence": float(system_exit.confidence or 0.0),
                    }
                )
                self._log.log_decision_trace(
                    self._build_position_trace(
                        snap=snap,
                        position=position,
                        evaluation_type="exit",
                        final_outcome="exit_taken",
                        exit_signal=system_exit,
                        primary_blocker_gate="position_exit",
                    )
                )
                return system_exit
            refreshed = self._tracker.current_position
            if refreshed is not None:
                self._log.log_position_manage(
                    position=refreshed,
                    timestamp=snap.timestamp_or_now,
                    snapshot_id=snap.snapshot_id,
                )
                self._last_event = {
                    "event": "position_manage",
                    "snapshot_id": snap.snapshot_id,
                    "position_id": refreshed.position_id,
                }
                self._last_decision = None
                self._session_updated_at_ist = isoformat_ist(snap.timestamp_or_now)
                self._write_runtime_state()
                self._append_runtime_metric(
                    {
                        "event": "position_manage",
                        "ts": isoformat_ist(snap.timestamp_or_now),
                        "bar": self._bars_evaluated,
                        "snapshot_id": snap.snapshot_id,
                        "position_id": refreshed.position_id,
                        "direction": refreshed.direction,
                        "strike": refreshed.strike,
                        "bars_held": refreshed.bars_held,
                        "pnl_pct": refreshed.pnl_pct,
                    }
                )
                self._log.log_decision_trace(
                    self._build_position_trace(
                        snap=snap,
                        position=refreshed,
                        evaluation_type="manage",
                        final_outcome="manage_only",
                        primary_blocker_gate=None,
                    )
                )
            return None

        decision = predict_staged(
            engine=self,
            snap=snap,
            rolling_features=rolling_features,
            bundle=self._model_package,
            policy=self._staged_runtime_policy,
        )
        self._last_decision = self._staged_decision_summary(decision)
        trace_builder = self._build_entry_trace_builder(snap=snap)
        self._populate_staged_candidate_trace(trace_builder, decision)
        if decision.action == "HOLD":
            self._log_hold(str(decision.reason), snap, staged_decision=decision)
            self._last_event = {
                "event": "hold",
                "snapshot_id": snap.snapshot_id,
                "reason": str(decision.reason),
            }
            self._session_updated_at_ist = isoformat_ist(snap.timestamp_or_now)
            self._write_runtime_state()
            blocker_gate = self._ml_blocker_gate(str(decision.reason))
            self._log.log_decision_trace(
                trace_builder.finalize(
                    final_outcome=("blocked" if blocker_gate not in {"stage1_threshold", "stage2_direction", "stage3_recipe"} else "hold"),
                    primary_blocker_gate=blocker_gate,
                    summary_metrics=self._staged_decision_metrics(decision),
                )
            )
            return None

        direction = "CE" if decision.action == "BUY_CE" else "PE"
        strike = snap.atm_strike
        if strike is None or int(strike) <= 0:
            self._log_hold("missing_atm_strike", snap, staged_decision=decision)
            self._last_event = {
                "event": "hold",
                "snapshot_id": snap.snapshot_id,
                "reason": "missing_atm_strike",
            }
            self._session_updated_at_ist = isoformat_ist(snap.timestamp_or_now)
            self._write_runtime_state()
            self._log.log_decision_trace(
                trace_builder.finalize(
                    final_outcome="blocked",
                    primary_blocker_gate="strike_selection",
                    summary_metrics=self._staged_decision_metrics(decision),
                )
            )
            return None
        premium = snap.option_ltp(direction, int(strike))
        if premium is None or premium <= 0:
            self._log_hold("missing_option_premium", snap, staged_decision=decision)
            self._last_event = {
                "event": "hold",
                "snapshot_id": snap.snapshot_id,
                "reason": "missing_option_premium",
            }
            self._session_updated_at_ist = isoformat_ist(snap.timestamp_or_now)
            self._write_runtime_state()
            self._log.log_decision_trace(
                trace_builder.finalize(
                    final_outcome="blocked",
                    primary_blocker_gate="option_premium",
                    summary_metrics=self._staged_decision_metrics(decision),
                )
            )
            return None

        stop_loss_pct = float(decision.stop_loss_pct or self._stop_loss_pct)
        target_pct = float(decision.target_pct or self._target_pct)
        max_hold_bars = int(decision.horizon_minutes or self._max_hold_bars)
        trace_builder.add_flow_gate(
            "lot_sizing",
            gate_group="execution",
            status="pass",
            metrics={
                "stop_loss_pct": stop_loss_pct,
                "target_pct": target_pct,
                "max_hold_bars": max_hold_bars,
            },
        )
        signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            timestamp=snap.timestamp_or_now,
            snapshot_id=snap.snapshot_id,
            signal_type=SignalType.ENTRY,
            direction=direction,
            strike=int(strike),
            entry_premium=float(premium),
            max_hold_bars=max_hold_bars,
            stop_loss_pct=stop_loss_pct,
            target_pct=target_pct,
            max_lots=self._risk.compute_lots(
                entry_premium=float(premium),
                stop_loss_pct=stop_loss_pct,
                confidence=float(max(decision.ce_prob, decision.pe_prob)),
            ),
            entry_strategy_name="ML_PURE_STAGED",
            entry_regime_name="staged_ml",
            source="ML_PURE",
            confidence=float(max(decision.ce_prob, decision.pe_prob)),
            reason=(
                f"ml_pure_staged: action={decision.action} entry_prob={decision.entry_prob:.4f} "
                f"dir_up_prob={decision.direction_up_prob:.4f} recipe={decision.recipe_id} "
                f"recipe_prob={decision.recipe_prob:.4f} recipe_margin={decision.recipe_margin:.4f} "
                f"reason={decision.reason}"
            ),
            votes=[],
        )
        self._annotate_signal_contract(
            signal,
            decision_reason_code=str(decision.reason),
            decision_metrics=self._staged_decision_metrics(decision),
        )
        opened = self._tracker.open_position(signal, snap)
        self._log.log_signal(signal, acted_on=True)
        self._log.log_position_open(signal, opened)
        self._entry_count += 1
        self._last_entry_at = isoformat_ist(snap.timestamp_or_now)
        self._last_event = {
            "event": "entry",
            "snapshot_id": snap.snapshot_id,
            "signal_id": signal.signal_id,
            "position_id": opened.position_id,
            "direction": signal.direction,
            "strike": signal.strike,
        }
        self._session_updated_at_ist = self._last_entry_at
        self._write_runtime_state()
        self._append_runtime_metric(
            {
                "event": "entry",
                "ts": isoformat_ist(snap.timestamp_or_now),
                "bar": self._bars_evaluated,
                "snapshot_id": snap.snapshot_id,
                "signal_id": signal.signal_id,
                "position_id": opened.position_id,
                "direction": signal.direction,
                "strike": signal.strike,
                "entry_premium": signal.entry_premium,
                "max_lots": signal.max_lots,
                "confidence": signal.confidence,
                "entry_prob": decision.entry_prob,
                "direction_up_prob": decision.direction_up_prob,
                "ce_prob": decision.ce_prob,
                "pe_prob": decision.pe_prob,
                "recipe_id": decision.recipe_id,
                "recipe_prob": decision.recipe_prob,
                "recipe_margin": decision.recipe_margin,
            }
        )
        self._log.log_decision_trace(
            trace_builder.finalize(
                final_outcome="entry_taken",
                primary_blocker_gate=None,
                summary_metrics={
                    **self._staged_decision_metrics(decision),
                    "entry_premium": premium,
                    "max_lots": signal.max_lots,
                },
            )
        )
        return signal

    def _liquidity_ok(self, *, snap: SnapshotAccessor, direction: str, strike: int) -> bool:
        oi = snap.option_oi(direction, strike)
        volume = snap.option_volume(direction, strike)
        if oi is None or volume is None:
            return False
        return float(oi) >= self._min_oi and float(volume) >= self._min_volume

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

    def _check_feature_freshness(self, snap: SnapshotAccessor) -> Optional[str]:
        if self._max_feature_age_sec <= 0:
            return None
        ts = snap.timestamp
        if ts is None:
            return "feature_stale"
        now = datetime.now(ts.tzinfo if ts.tzinfo is not None else timezone.utc)
        age = (now - ts).total_seconds()
        if age > float(self._max_feature_age_sec):
            return "feature_stale"
        return None

    def _annotate_signal_contract(
        self,
        signal: TradeSignal,
        *,
        decision_reason_code: Optional[str],
        decision_metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        apply_signal_contract(
            signal,
            engine_mode=self._engine_mode,
            strategy_family_version=self._strategy_family_version,
            strategy_profile_id=self._strategy_profile_id,
            decision_mode="ml_staged",
            decision_reason_code=decision_reason_code,
            decision_metrics=decision_metrics,
        )

    def _staged_decision_metrics(self, decision: StagedRuntimeDecision) -> dict[str, float]:
        return {
            "entry_prob": float(decision.entry_prob),
            "direction_up_prob": float(decision.direction_up_prob),
            "ce_prob": float(decision.ce_prob),
            "pe_prob": float(decision.pe_prob),
            "recipe_prob": float(decision.recipe_prob),
            "recipe_margin": float(decision.recipe_margin),
        }

    def _staged_decision_summary(self, decision: StagedRuntimeDecision) -> dict[str, Any]:
        summary = asdict(decision)
        summary["action"] = str(summary.get("action") or "")
        summary["reason"] = str(summary.get("reason") or "")
        return summary

    def _warmup_state(self) -> dict[str, Any]:
        elapsed_minutes = 0.0
        if self._session_start_monotonic is not None:
            elapsed_minutes = max(0.0, (wall_time.monotonic() - self._session_start_monotonic) / 60.0)
        return {
            "events_seen": int(self._session_event_count),
            "minutes_elapsed": float(elapsed_minutes),
            "min_events": int(self._startup_warmup_events),
            "min_minutes": float(self._startup_warmup_minutes),
        }

    def _runtime_state_payload(self) -> dict[str, Any]:
        current_position = self._tracker.current_position
        return build_runtime_state_payload(
            engine=self._engine_mode,
            strategy_profile_id=self._strategy_profile_id,
            runtime_artifact_dir=self._runtime_artifacts.paths.root,
            run_id=self._model_run_id or self._run_id,
            model_group=self._model_group,
            block_expiry=self._runtime_controls.block_expiry,
            max_feature_age_sec=self._max_feature_age_sec,
            max_nan_features=self._max_nan_features,
            max_hold_bars=self._max_hold_bars,
            min_oi=self._min_oi,
            min_volume=self._min_volume,
            session_trade_date=self._session_trade_date,
            session_started_at_ist=self._session_started_at_ist,
            session_updated_at_ist=self._session_updated_at_ist,
            bars_evaluated=self._bars_evaluated,
            entries_taken=self._entry_count,
            last_entry_at=self._last_entry_at,
            hold_counts=self._hold_counts,
            is_halted=self._risk.is_halted,
            is_paused=getattr(self._risk, "is_paused", False),
            session_pnl_pct=getattr(self._risk, "session_pnl_pct", None),
            consecutive_losses=getattr(self._risk, "consecutive_losses", 0),
            has_position=self._tracker.has_position,
            current_position=asdict(current_position) if current_position is not None else None,
            last_event=self._last_event,
            last_decision=self._last_decision,
            warmup=self._warmup_state(),
        )

    def _write_runtime_state(
        self,
        *,
        last_event: Optional[dict[str, Any]] = None,
        last_decision: Optional[dict[str, Any]] = None,
    ) -> None:
        if last_event is not None:
            self._last_event = dict(last_event)
        if last_decision is not None:
            self._last_decision = dict(last_decision)
        self._runtime_artifacts.write_state(self._runtime_state_payload())

    def _append_runtime_metric(self, payload: dict[str, Any]) -> None:
        metric = dict(payload)
        metric.setdefault("engine", self._engine_mode)
        metric.setdefault("strategy_profile_id", self._strategy_profile_id)
        metric.setdefault("run_id", self._model_run_id or self._run_id)
        metric.setdefault("model_group", self._model_group)
        metric.setdefault("session_trade_date", self._session_trade_date)
        metric.setdefault("bar", self._bars_evaluated)
        self._runtime_artifacts.append_metric(metric)

    def _log_hold(
        self,
        reason: str,
        snap: SnapshotAccessor,
        staged_decision: Optional[StagedRuntimeDecision] = None,
    ) -> None:
        hold_signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            timestamp=snap.timestamp_or_now,
            snapshot_id=snap.snapshot_id,
            signal_type=SignalType.HOLD,
            source="ML_PURE",
            confidence=(
                float(max(staged_decision.ce_prob, staged_decision.pe_prob))
                if staged_decision is not None
                else None
            ),
            reason=f"ml_pure_hold:{str(reason).strip().lower()}",
            votes=[],
        )
        self._annotate_signal_contract(
            hold_signal,
            decision_reason_code=str(reason).strip().lower(),
            decision_metrics=(self._staged_decision_metrics(staged_decision) if staged_decision is not None else None),
        )
        self._log.log_signal(hold_signal, acted_on=False)
        hold_reason = str(reason).strip().lower()
        self._hold_counts[hold_reason] = int(self._hold_counts.get(hold_reason, 0)) + 1
        self._last_event = {
            "event": "hold",
            "snapshot_id": snap.snapshot_id,
            "reason": hold_reason,
        }
        self._session_updated_at_ist = isoformat_ist(snap.timestamp_or_now)
        self._append_runtime_metric(
            {
                "event": "hold",
                "ts": isoformat_ist(snap.timestamp_or_now),
                "bar": self._bars_evaluated,
                "snapshot_id": snap.snapshot_id,
                "reason": hold_reason,
                "confidence": hold_signal.confidence,
                "entry_prob": (staged_decision.entry_prob if staged_decision is not None else None),
                "direction_up_prob": (staged_decision.direction_up_prob if staged_decision is not None else None),
                "ce_prob": (staged_decision.ce_prob if staged_decision is not None else None),
                "pe_prob": (staged_decision.pe_prob if staged_decision is not None else None),
                "recipe_id": (staged_decision.recipe_id if staged_decision is not None else None),
                "recipe_prob": (staged_decision.recipe_prob if staged_decision is not None else None),
                "recipe_margin": (staged_decision.recipe_margin if staged_decision is not None else None),
            }
        )
        self._write_runtime_state()
        logger.debug("ml_pure hold reason=%s snapshot_id=%s", reason, snap.snapshot_id)

    def _build_entry_trace_builder(self, *, snap: SnapshotAccessor) -> DecisionTraceBuilder:
        regime_signal = self._regime.classify(snap)
        builder = DecisionTraceBuilder(
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            engine_mode=self._engine_mode,
            decision_mode="ml_staged",
            evaluation_type="entry",
            run_id=self._model_run_id or self._run_id,
        )
        warmup_blocked, warmup_reason = self._entry_warmup_status()
        builder.set_context(
            position_state=position_state_payload(None),
            risk_state=risk_state_payload(self._risk),
            regime_context=regime_context_payload(regime_signal),
            warmup_context=warmup_context_payload(
                blocked=warmup_blocked,
                reason=warmup_reason,
                state=self._warmup_state(),
            ),
        )
        return builder

    def _populate_staged_candidate_trace(
        self,
        builder: DecisionTraceBuilder,
        decision: StagedRuntimeDecision,
    ) -> None:
        candidate = builder.add_candidate(
            strategy_name="ML_PURE_STAGED",
            candidate_type="staged_runtime",
            direction=("CE" if decision.action == "BUY_CE" else ("PE" if decision.action == "BUY_PE" else None)),
            confidence=max(decision.ce_prob, decision.pe_prob, decision.entry_prob, 0.0),
            rank=1,
            metrics=self._staged_decision_metrics(decision),
        )
        blocker_gate = self._ml_blocker_gate(str(decision.reason))
        flow = self._ml_trace_flow(str(decision.reason), decision)
        for gate in flow:
            builder.add_candidate_gate(candidate, **gate)
            builder.add_flow_gate(**gate)
        builder.finalize_candidate(
            candidate,
            terminal_status=("passed" if decision.action != "HOLD" else ("blocked" if blocker_gate not in {"stage1_threshold", "stage2_direction", "stage3_recipe"} else "skipped")),
            terminal_gate_id=(None if decision.action != "HOLD" else blocker_gate),
            terminal_reason_code=(None if decision.action != "HOLD" else str(decision.reason)),
            selected=(decision.action != "HOLD"),
        )

    def _ml_trace_flow(self, reason: str, decision: StagedRuntimeDecision) -> list[dict[str, Any]]:
        blocker_gate = self._ml_blocker_gate(reason)
        metrics = self._staged_decision_metrics(decision)
        flow: list[dict[str, Any]] = []
        ordered = [
            ("prefilter", "prefilter"),
            ("stage1_threshold", "stage1"),
            ("stage2_direction", "stage2"),
            ("strike_selection", "execution"),
            ("liquidity_gate", "execution"),
            ("stage3_recipe", "stage3"),
            ("option_premium", "execution"),
            ("lot_sizing", "execution"),
        ]
        for gate_id, gate_group in ordered:
            if blocker_gate == gate_id:
                flow.append(
                    {
                        "gate_id": gate_id,
                        "gate_group": gate_group,
                        "status": "blocked",
                        "reason_code": reason,
                        "message": reason,
                        "metrics": metrics,
                    }
                )
                break
            flow.append(
                {
                    "gate_id": gate_id,
                    "gate_group": gate_group,
                    "status": "pass",
                    "reason_code": None,
                    "message": None,
                    "metrics": (metrics if gate_id in {"stage1_threshold", "stage2_direction", "stage3_recipe"} else None),
                }
            )
        return flow

    def _ml_blocker_gate(self, reason: str) -> str:
        code = str(reason or "").strip().lower()
        if code in {
            "risk_halt",
            "risk_pause",
            "entry_warmup_block",
            "feature_stale",
            "feature_incomplete",
            "stage2_feature_incomplete",
            "stage3_feature_incomplete",
            "regime_sideways",
            "regime_avoid",
            "regime_expiry",
            "regime_low_confidence",
            "invalid_entry_phase",
        }:
            return "prefilter"
        if code == "entry_below_threshold":
            return "stage1_threshold"
        if code in {"direction_below_threshold", "direction_low_edge_conflict"}:
            return "stage2_direction"
        if code == "missing_atm_strike":
            return "strike_selection"
        if code == "liquidity_gate_block":
            return "liquidity_gate"
        if code in {"recipe_below_threshold", "recipe_low_margin", "recipe_scores_missing"}:
            return "stage3_recipe"
        if code == "missing_option_premium":
            return "option_premium"
        return "prefilter"

    def _build_position_trace(
        self,
        *,
        snap: SnapshotAccessor,
        position: PositionContext,
        evaluation_type: str,
        final_outcome: str,
        primary_blocker_gate: Optional[str],
        exit_signal: Optional[TradeSignal] = None,
    ) -> dict[str, Any]:
        builder = DecisionTraceBuilder(
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            engine_mode=self._engine_mode,
            decision_mode="ml_staged",
            evaluation_type=evaluation_type,
            run_id=self._model_run_id or self._run_id,
        )
        builder.set_context(
            position_state=position_state_payload(position),
            risk_state=risk_state_payload(self._risk),
            regime_context=regime_context_payload(self._regime.classify(snap)),
            warmup_context=warmup_context_payload(blocked=False, reason=None, state=self._warmup_state()),
        )
        candidate = builder.add_candidate(
            strategy_name=str(position.entry_strategy or "ML_PURE_STAGED"),
            candidate_type="position",
            direction=position.direction,
            confidence=(exit_signal.confidence if exit_signal is not None else None),
            rank=1,
            metrics=compact_metrics(position.decision_metrics if isinstance(position.decision_metrics, dict) else {}),
        )
        gate_reason = exit_signal.exit_reason.value.lower() if exit_signal is not None and exit_signal.exit_reason else None
        builder.add_candidate_gate(
            candidate,
            "position_tracker",
            gate_group="position",
            status=("pass" if final_outcome in {"manage_only", "exit_taken"} else "blocked"),
            reason_code=gate_reason,
            message=(str(exit_signal.reason or "").strip() if exit_signal is not None else None),
            metrics={"bars_held": position.bars_held, "pnl_pct": position.pnl_pct},
        )
        builder.finalize_candidate(
            candidate,
            terminal_status=("passed" if final_outcome in {"manage_only", "exit_taken"} else "blocked"),
            terminal_gate_id=primary_blocker_gate,
            terminal_reason_code=gate_reason,
            selected=(final_outcome == "exit_taken"),
        )
        return builder.finalize(
            final_outcome=final_outcome,
            primary_blocker_gate=primary_blocker_gate,
            summary_metrics={
                "bars_held": position.bars_held,
                "pnl_pct": position.pnl_pct,
                "current_premium": position.current_premium,
            },
        )

    @staticmethod
    def _merge_feature_rows(base: dict[str, object], computed: dict[str, object]) -> dict[str, object]:
        if not computed:
            return base
        out = dict(base)
        for key, value in computed.items():
            if value is None:
                continue
            try:
                f = float(value)
            except Exception:
                out[str(key)] = value
                continue
            if np.isfinite(f):
                out[str(key)] = float(f)
        return out

    def _handle_position_closed(self, exit_signal: TradeSignal, position: PositionContext) -> None:
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


def _session_end_snapshot(trade_date: date) -> SnapshotAccessor:
    return SnapshotAccessor(
        {
            "snapshot_id": f"SESSION_END_{trade_date.isoformat()}",
            "session_context": {
                "snapshot_id": f"SESSION_END_{trade_date.isoformat()}",
                "timestamp": f"{trade_date.isoformat()}T15:15:00+05:30",
                "date": trade_date.isoformat(),
                "session_phase": "PRE_CLOSE",
            },
        }
    )

"""Pure-ML runtime engine (staged runtime bundle only)."""

from __future__ import annotations

import logging
import os
import time as wall_time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

import numpy as np

from ..contracts import ExitReason, PositionContext, SignalType, SnapshotPayload, StrategyEngine, TradeSignal
from ..logging.signal_logger import SignalLogger
from ..position.tracker import PositionTracker
from ..risk.manager import RiskManager
from .decision_annotation import annotate_signal_contract as apply_signal_contract
from .pure_ml_staged_runtime import PureMLRuntimeControls, StagedRuntimeDecision, load_staged_model_package, load_staged_policy, predict_staged
from .rolling_feature_state import RollingFeatureState
from .regime import RegimeClassifier
from .snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)


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
        stop_loss_pct: float = 0.05,
        target_pct: float = 0.20,
        signal_logger: Optional[SignalLogger] = None,
        strategy_profile_id: Optional[str] = None,
    ) -> None:
        self._model_package = load_staged_model_package(model_package_path)
        self._staged_runtime_policy: dict[str, Any] = load_staged_policy(threshold_report_path)
        runtime_payload = dict(self._staged_runtime_policy.get("runtime") or {})
        bundle_runtime_payload = dict(self._model_package.get("runtime") or {})
        self._runtime_controls = PureMLRuntimeControls(
            block_expiry=bool(runtime_payload.get("block_expiry", bundle_runtime_payload.get("block_expiry", False))),
        )
        self._regime = RegimeClassifier()
        self._feature_state = RollingFeatureState()
        self._tracker = PositionTracker()
        self._risk = RiskManager()
        self._log = signal_logger or SignalLogger()
        self._max_feature_age_sec = max(0, int(max_feature_age_sec))
        self._max_nan_features = max(0, int(max_nan_features))
        self._max_hold_bars = max(1, int(max_hold_bars))
        self._min_oi = max(0.0, float(min_oi))
        self._min_volume = max(0.0, float(min_volume))
        self._stop_loss_pct = max(0.0, float(stop_loss_pct))
        self._target_pct = max(0.0, float(target_pct))
        self._startup_warmup_minutes = max(0.0, float(os.getenv("STRATEGY_STARTUP_WARMUP_MINUTES", "0") or 0.0))
        self._startup_warmup_events = max(0, int(os.getenv("STRATEGY_STARTUP_WARMUP_EVENTS", "0") or 0))
        self._session_start_monotonic: Optional[float] = None
        self._session_event_count = 0
        self._engine_mode = "ml_pure"
        self._strategy_family_version = "ML_PURE_STAGED_V1"
        default_profile_id = "ml_pure_staged_v1"
        self._strategy_profile_id = str(strategy_profile_id or default_profile_id).strip() or default_profile_id
        self._set_logger_context(None)
        logger.info(
            "pure ml staged engine initialized max_feature_age_sec=%d max_nan_features=%d min_oi=%.0f min_volume=%.0f block_expiry=%s",
            self._max_feature_age_sec,
            self._max_nan_features,
            self._min_oi,
            self._min_volume,
            str(self._runtime_controls.block_expiry).lower(),
        )

    def set_run_context(self, run_id: Optional[str], metadata: Optional[dict[str, Any]] = None) -> None:
        if isinstance(metadata, dict):
            profile = str(metadata.get("strategy_profile_id") or "").strip()
            if profile:
                self._strategy_profile_id = profile
            regime_payload = metadata.get("regime_config")
            if isinstance(regime_payload, dict):
                self._regime.configure(regime_payload)
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

    def on_session_start(self, trade_date: date) -> None:
        self._session_start_monotonic = wall_time.monotonic()
        self._session_event_count = 0
        self._feature_state.on_session_start(trade_date)
        self._tracker.on_session_start(trade_date)
        self._risk.on_session_start(trade_date)
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
        self._feature_state.on_session_end()
        self._risk.on_session_end(trade_date)
        self._session_start_monotonic = None
        self._session_event_count = 0
        logger.info("pure ml engine session ended: %s", trade_date.isoformat())

    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        self._session_event_count += 1
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
                return system_exit
            refreshed = self._tracker.current_position
            if refreshed is not None:
                self._log.log_position_manage(
                    position=refreshed,
                    timestamp=snap.timestamp_or_now,
                    snapshot_id=snap.snapshot_id,
                )
            return None

        decision = predict_staged(
            engine=self,
            snap=snap,
            rolling_features=rolling_features,
            bundle=self._model_package,
            policy=self._staged_runtime_policy,
        )
        if decision.action == "HOLD":
            self._log_hold(str(decision.reason), snap, staged_decision=decision)
            return None

        direction = "CE" if decision.action == "BUY_CE" else "PE"
        strike = snap.atm_strike
        if strike is None or int(strike) <= 0:
            self._log_hold("missing_atm_strike", snap, staged_decision=decision)
            return None
        premium = snap.option_ltp(direction, int(strike))
        if premium is None or premium <= 0:
            self._log_hold("missing_option_premium", snap, staged_decision=decision)
            return None

        stop_loss_pct = float(decision.stop_loss_pct or self._stop_loss_pct)
        target_pct = float(decision.target_pct or self._target_pct)
        max_hold_bars = int(decision.horizon_minutes or self._max_hold_bars)
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
        logger.debug("ml_pure hold reason=%s snapshot_id=%s", reason, snap.snapshot_id)

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

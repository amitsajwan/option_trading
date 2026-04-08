"""Deterministic strategy implementations."""

from __future__ import annotations

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
from ..snapshot_accessor import SnapshotAccessor
from ..trader_judgement import (
    OptionTradabilityScorer,
    TradeGovernor,
    TraderAction,
    TraderAnnotationRecord,
    TraderDayClassifier,
    TraderDayType,
    TraderSetupScorer,
    TraderSetupState,
    TraderSetupType,
)
from ..trader_v3 import TraderV3CompositeStrategy


class _TraderSetupStrategy(BaseStrategy):
    """Shared helpers for setup-style option entries."""

    @staticmethod
    def _option_liquidity_ratio(snap: SnapshotAccessor, direction: Direction) -> Optional[float]:
        return snap.atm_ce_vol_ratio if direction == Direction.CE else snap.atm_pe_vol_ratio

    @staticmethod
    def _directional_premium(snap: SnapshotAccessor, direction: Direction) -> Optional[float]:
        return snap.atm_ce_close if direction == Direction.CE else snap.atm_pe_close

    @staticmethod
    def _oi_change_pct(snap: SnapshotAccessor) -> Optional[float]:
        if snap.fut_oi_change_30m is None or snap.fut_oi is None or snap.fut_oi <= 0:
            return None
        return snap.fut_oi_change_30m / snap.fut_oi

    @staticmethod
    def _oi_confirms(oi_change_pct: Optional[float], threshold: float) -> bool:
        return oi_change_pct is None or oi_change_pct >= threshold

    def _entry_vote(
        self,
        snap: SnapshotAccessor,
        *,
        direction: Direction,
        confidence: float,
        reason: str,
        raw_signals: dict[str, Any],
    ) -> StrategyVote:
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=direction,
            confidence=round(min(1.0, confidence), 2),
            reason=reason,
            raw_signals=raw_signals,
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=self._directional_premium(snap, direction),
        )

    def _exit_vote(
        self,
        snap: SnapshotAccessor,
        *,
        confidence: float,
        reason: str,
        raw_signals: dict[str, Any],
        exit_reason: ExitReason = ExitReason.REGIME_SHIFT,
    ) -> StrategyVote:
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=round(min(1.0, confidence), 2),
            reason=reason,
            raw_signals=raw_signals,
            exit_reason=exit_reason,
        )


class TraderCompositeStrategy(_TraderSetupStrategy):
    """Composite trader-style decision layer using day/setup/option scoring."""

    name = "TRADER_COMPOSITE"

    def __init__(self) -> None:
        self._day_classifier = TraderDayClassifier()
        self._setup_scorer = TraderSetupScorer()
        self._option_scorer = OptionTradabilityScorer()
        self._trade_governor = TradeGovernor()
        self._state = TraderSetupState()
        self._entries_taken = 0
        self._trade_date: Optional[str] = None

    def on_session_start(self, trade_date) -> None:
        self._trade_date = str(trade_date)
        self._state = TraderSetupState()
        self._entries_taken = 0

    def on_session_end(self, trade_date) -> None:
        self.on_session_start(trade_date)

    def _ensure_session(self, snap: SnapshotAccessor) -> None:
        if self._trade_date != snap.trade_date:
            self.on_session_start(snap.trade_date)

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        self._ensure_session(snap)

        if position is not None:
            return self._check_exit(snap, position)
        if not snap.is_valid_entry_phase:
            return None

        day = self._day_classifier.assess(snap)
        if day.day_type == TraderDayType.NO_TRADE:
            annotation = TraderAnnotationRecord(
                snapshot_id=snap.snapshot_id,
                trade_date=snap.trade_date,
                day_type=day.day_type.value,
                setup_type=TraderSetupType.NONE.value,
                action=TraderAction.SKIP.value,
                notes="day classifier marked session as no-trade",
            )
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.SKIP,
                direction=Direction.AVOID,
                confidence=round(day.score, 2),
                reason=f"TRADER_SKIP: day_type={day.day_type.value}",
                raw_signals={"day_type": day.day_type.value, "day_score": day.score, "annotation": annotation.to_payload()},
            )

        self._setup_scorer.observe(snap, self._state)
        setup = self._setup_scorer.best_setup(snap, self._state, day)
        if not setup.trigger_ready or setup.direction is None:
            return None

        option = self._option_scorer.assess(snap, setup.direction, expected_move_pct=setup.expected_move_pct)
        if not option.tradable:
            return None
        governor = self._trade_governor.evaluate(
            day=day,
            setup=setup,
            option=option,
            entries_taken=self._entries_taken,
        )
        if not governor.allowed:
            if governor.reason in {"no_trade_day", "balanced_day_skip"}:
                annotation = TraderAnnotationRecord(
                    snapshot_id=snap.snapshot_id,
                    trade_date=snap.trade_date,
                    day_type=day.day_type.value,
                    setup_type=setup.setup_type.value,
                    action=TraderAction.SKIP.value,
                    direction=setup.direction.value if setup.direction is not None else None,
                    option_plan="ATM",
                    invalidation_reference=setup.invalidation_reference,
                    notes=f"governor={governor.reason}",
                )
                return StrategyVote(
                    strategy_name=self.name,
                    snapshot_id=snap.snapshot_id,
                    timestamp=snap.timestamp_or_now,
                    trade_date=snap.trade_date,
                    signal_type=SignalType.SKIP,
                    direction=Direction.AVOID,
                    confidence=round(max(day.score, setup.score), 2),
                    reason=f"TRADER_SKIP: {governor.reason}",
                    raw_signals={
                        "day_type": day.day_type.value,
                        "day_score": day.score,
                        "setup_type": setup.setup_type.value,
                        "setup_score": setup.score,
                        "option_score": option.score,
                        "governor_reason": governor.reason,
                        "entries_taken": self._entries_taken,
                        "max_entries": governor.max_entries,
                        "annotation": annotation.to_payload(),
                    },
                )
            return None

        confidence = min(0.96, (0.35 * day.score) + (0.45 * setup.score) + (0.20 * option.score))
        annotation = TraderAnnotationRecord(
            snapshot_id=snap.snapshot_id,
            trade_date=snap.trade_date,
            day_type=day.day_type.value,
            setup_type=setup.setup_type.value,
            action=TraderAction.TAKE.value,
            direction=setup.direction.value,
            option_plan="ATM",
            invalidation_reference=setup.invalidation_reference,
            notes="composite trader-style judgement",
        )
        self._entries_taken += 1
        self._state = TraderSetupState()
        return self._entry_vote(
            snap,
            direction=setup.direction,
            confidence=confidence,
            reason=(
                f"TRADER_{setup.setup_type.value}: day={day.day_type.value} "
                f"day_score={day.score:.2f} setup_score={setup.score:.2f} option_score={option.score:.2f}"
            ),
            raw_signals={
                "day_type": day.day_type.value,
                "day_score": day.score,
                "day_reasons": list(day.reasons),
                "setup_type": setup.setup_type.value,
                "setup_score": setup.score,
                "setup_reasons": list(setup.reasons),
                "invalidation_reference": setup.invalidation_reference,
                "expected_move_pct": setup.expected_move_pct,
                "option_score": option.score,
                "option_reasons": list(option.reasons),
                "option_premium": option.premium,
                "option_liquidity_ratio": option.liquidity_ratio,
                "governor_reason": governor.reason,
                "entries_taken": self._entries_taken,
                "max_entries": governor.max_entries,
                "annotation": annotation.to_payload(),
            },
        )

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        entry_reason = str(position.entry_reason or "").upper()
        if "ORB_RETEST" in entry_reason:
            return self._exit_orb_retest(snap, position)
        if "VWAP_PULLBACK" in entry_reason:
            return self._exit_vwap_pullback(snap, position)
        if "FAILED_BREAKOUT" in entry_reason:
            return self._exit_failed_breakout(snap, position)
        return self._exit_vwap_pullback(snap, position)

    def _exit_orb_retest(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if not snap.or_ready or snap.fut_close is None or snap.orh is None or snap.orl is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.orh * 0.9988:
            return self._exit_vote(
                snap,
                confidence=0.82,
                reason=f"TRADER_EXIT_ORB_RETEST_LONG: close={snap.fut_close:.0f}<orh={snap.orh:.0f}",
                raw_signals={"close": snap.fut_close, "orh": snap.orh, "setup": "ORB_RETEST"},
                exit_reason=ExitReason.STRATEGY_EXIT,
            )
        if position.direction == "PE" and snap.fut_close > snap.orl * 1.0012:
            return self._exit_vote(
                snap,
                confidence=0.82,
                reason=f"TRADER_EXIT_ORB_RETEST_SHORT: close={snap.fut_close:.0f}>orl={snap.orl:.0f}",
                raw_signals={"close": snap.fut_close, "orl": snap.orl, "setup": "ORB_RETEST"},
                exit_reason=ExitReason.STRATEGY_EXIT,
            )
        return None

    def _exit_vwap_pullback(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if snap.fut_close is None or snap.vwap is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.vwap * 0.9992:
            return self._exit_vote(
                snap,
                confidence=0.78,
                reason=f"TRADER_EXIT_VWAP_LONG: close={snap.fut_close:.0f}<vwap={snap.vwap:.0f}",
                raw_signals={"close": snap.fut_close, "vwap": snap.vwap, "setup": "VWAP_PULLBACK"},
                exit_reason=ExitReason.STRATEGY_EXIT,
            )
        if position.direction == "PE" and snap.fut_close > snap.vwap * 1.0008:
            return self._exit_vote(
                snap,
                confidence=0.78,
                reason=f"TRADER_EXIT_VWAP_SHORT: close={snap.fut_close:.0f}>vwap={snap.vwap:.0f}",
                raw_signals={"close": snap.fut_close, "vwap": snap.vwap, "setup": "VWAP_PULLBACK"},
                exit_reason=ExitReason.STRATEGY_EXIT,
            )
        return None

    def _exit_failed_breakout(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if snap.fut_close is None or snap.orh is None or snap.orl is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.orl * 0.9988:
            return self._exit_vote(
                snap,
                confidence=0.77,
                reason=f"TRADER_EXIT_FAILED_BREAKOUT_LONG: close={snap.fut_close:.0f}<orl={snap.orl:.0f}",
                raw_signals={"close": snap.fut_close, "orl": snap.orl, "setup": "FAILED_BREAKOUT"},
                exit_reason=ExitReason.STRATEGY_EXIT,
            )
        if position.direction == "PE" and snap.fut_close > snap.orh * 1.0012:
            return self._exit_vote(
                snap,
                confidence=0.77,
                reason=f"TRADER_EXIT_FAILED_BREAKOUT_SHORT: close={snap.fut_close:.0f}>orh={snap.orh:.0f}",
                raw_signals={"close": snap.fut_close, "orh": snap.orh, "setup": "FAILED_BREAKOUT"},
                exit_reason=ExitReason.STRATEGY_EXIT,
            )
        return None


class ORBStrategy(BaseStrategy):
    """Opening range breakout with volume and PCR confirmation."""

    name = "ORB"

    def __init__(
        self,
        *,
        vol_ratio_min: float = 1.8,
        pcr_bull_min: float = 1.00,
        pcr_bear_max: float = 1.00,
        max_entry_minute: int = 105,
        confidence_base: float = 0.78,
        breakout_buffer_pct: float = 0.0005,
        min_r5m_confirm: float = 0.0008,
        exit_buffer_pct: float = 0.002,
    ) -> None:
        self._vol_ratio_min = vol_ratio_min
        self._pcr_bull_min = pcr_bull_min
        self._pcr_bear_max = pcr_bear_max
        self._max_entry_minute = max_entry_minute
        self._confidence_base = confidence_base
        self._breakout_buffer_pct = max(0.0, float(breakout_buffer_pct))
        self._min_r5m_confirm = max(0.0, float(min_r5m_confirm))
        self._exit_buffer_pct = max(0.0, float(exit_buffer_pct))

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)

        if position is not None:
            return self._check_exit(snap, position)

        if not snap.is_valid_entry_phase or snap.minutes > self._max_entry_minute or not snap.or_ready:
            return None

        close = snap.fut_close
        vol_ratio = snap.vol_ratio
        pcr = snap.pcr
        r5m = snap.fut_return_5m
        orh = snap.orh
        orl = snap.orl
        if close is None or orh is None or orl is None or r5m is None:
            return None

        if snap.orh_broken and close > (orh * (1.0 + self._breakout_buffer_pct)) and r5m >= self._min_r5m_confirm:
            if vol_ratio is None or vol_ratio < self._vol_ratio_min:
                return None
            if pcr is None or pcr < self._pcr_bull_min:
                return None
            confidence = self._confidence_base
            reasons = [f"close={close:.0f}>orh={orh:.0f}", f"r5m={r5m:.4f}"]
            if vol_ratio >= self._vol_ratio_min:
                confidence += 0.10
                reasons.append(f"vol_ratio={vol_ratio:.2f}")
            if pcr >= self._pcr_bull_min:
                confidence += 0.05
                reasons.append(f"pcr={pcr:.2f}")
            if confidence >= 0.50:
                return self._entry_vote(
                    snap,
                    direction=Direction.CE,
                    confidence=min(1.0, confidence),
                    reason="ORB_UP: " + ", ".join(reasons),
                    premium=snap.atm_ce_close,
                    raw_signals={"close": close, "orh": orh, "orl": orl, "vol_ratio": vol_ratio, "pcr": pcr, "r5m": r5m},
                )

        if snap.orl_broken and close < (orl * (1.0 - self._breakout_buffer_pct)) and r5m <= -self._min_r5m_confirm:
            if vol_ratio is None or vol_ratio < self._vol_ratio_min:
                return None
            if pcr is None or pcr > self._pcr_bear_max:
                return None
            confidence = self._confidence_base
            reasons = [f"close={close:.0f}<orl={orl:.0f}", f"r5m={r5m:.4f}"]
            if vol_ratio >= self._vol_ratio_min:
                confidence += 0.10
                reasons.append(f"vol_ratio={vol_ratio:.2f}")
            if pcr <= self._pcr_bear_max:
                confidence += 0.05
                reasons.append(f"pcr={pcr:.2f}")
            if confidence >= 0.50:
                return self._entry_vote(
                    snap,
                    direction=Direction.PE,
                    confidence=min(1.0, confidence),
                    reason="ORB_DOWN: " + ", ".join(reasons),
                    premium=snap.atm_pe_close,
                    raw_signals={"close": close, "orh": orh, "orl": orl, "vol_ratio": vol_ratio, "pcr": pcr, "r5m": r5m},
                )
        return None

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if not snap.or_ready or snap.fut_close is None or snap.orh is None or snap.orl is None:
            return None
        ce_exit_level = snap.orh * (1.0 - self._exit_buffer_pct)
        pe_exit_level = snap.orl * (1.0 + self._exit_buffer_pct)
        if position.direction == "CE" and snap.fut_close < ce_exit_level:
            return self._exit_vote(
                snap,
                f"ORB_REGIME_SHIFT: close={snap.fut_close:.0f}<orh_buffer={ce_exit_level:.0f}",
                ExitReason.REGIME_SHIFT,
                {"close": snap.fut_close, "orh": snap.orh, "orl": snap.orl, "exit_buffer_pct": self._exit_buffer_pct},
            )
        if position.direction == "PE" and snap.fut_close > pe_exit_level:
            return self._exit_vote(
                snap,
                f"ORB_REGIME_SHIFT: close={snap.fut_close:.0f}>orl_buffer={pe_exit_level:.0f}",
                ExitReason.REGIME_SHIFT,
                {"close": snap.fut_close, "orh": snap.orh, "orl": snap.orl, "exit_buffer_pct": self._exit_buffer_pct},
            )
        return None

    def _entry_vote(
        self,
        snap: SnapshotAccessor,
        *,
        direction: Direction,
        confidence: float,
        reason: str,
        premium: Optional[float],
        raw_signals: dict[str, object],
    ) -> StrategyVote:
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=direction,
            confidence=round(confidence, 2),
            reason=reason,
            raw_signals=raw_signals,
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=premium,
        )

    def _exit_vote(
        self,
        snap: SnapshotAccessor,
        reason: str,
        exit_reason: ExitReason,
        raw_signals: dict[str, object],
    ) -> StrategyVote:
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.80,
            reason=reason,
            raw_signals=raw_signals,
            exit_reason=exit_reason,
        )


class HighVolORBStrategy(ORBStrategy):
    """ORB profile restricted for HIGH_VOL regime routing."""

    name = "HIGH_VOL_ORB"

    def __init__(self) -> None:
        super().__init__(
            vol_ratio_min=1.8,
            pcr_bull_min=0.90,
            pcr_bear_max=1.10,
            max_entry_minute=90,
            confidence_base=0.70,
        )


class OIBuildupStrategy(BaseStrategy):
    """Directional OI build-up signal."""

    name = "OI_BUILDUP"

    def __init__(
        self,
        *,
        oi_change_threshold: float = 0.03,
        confidence_base: float = 0.72,
        min_entry_minute: int = 45,
        max_entry_minute: int = 210,
        min_directional_r15m: float = 0.0015,
        min_vol_ratio: float = 1.35,
        pcr_bull_min: float = 1.00,
        pcr_bear_max: float = 1.00,
        exit_r5m_threshold: float = 0.0003,
        min_exit_hold_bars: int = 3,
    ) -> None:
        self._oi_change_threshold = oi_change_threshold
        self._confidence_base = confidence_base
        self._min_entry_minute = max(0, int(min_entry_minute))
        self._max_entry_minute = max(self._min_entry_minute, int(max_entry_minute))
        self._min_directional_r15m = max(0.0, float(min_directional_r15m))
        self._min_vol_ratio = max(0.0, float(min_vol_ratio))
        self._pcr_bull_min = float(pcr_bull_min)
        self._pcr_bear_max = float(pcr_bear_max)
        self._exit_r5m_threshold = float(exit_r5m_threshold)
        self._min_exit_hold_bars = max(0, int(min_exit_hold_bars))

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)

        if position is not None:
            return self._check_exit(snap, position)

        if not snap.is_valid_entry_phase or snap.minutes < self._min_entry_minute or snap.minutes > self._max_entry_minute:
            return None

        oi_change = snap.fut_oi_change_30m
        fut_oi = snap.fut_oi
        r15m = snap.fut_return_15m
        if oi_change is None or fut_oi is None or fut_oi <= 0 or r15m is None:
            return None

        oi_change_pct = oi_change / fut_oi
        pcr = snap.pcr
        vol_ratio = snap.vol_ratio
        confidence = self._confidence_base
        if vol_ratio is None or vol_ratio < self._min_vol_ratio:
            return None

        if oi_change_pct > self._oi_change_threshold and r15m > self._min_directional_r15m and pcr is not None and pcr >= self._pcr_bull_min:
            reasons = [f"oi_chg={oi_change_pct:.2%}", f"r15m={r15m:.4f}"]
            confidence += 0.05
            reasons.append(f"pcr={pcr:.2f}")
            if vol_ratio > self._min_vol_ratio:
                confidence += 0.05
                reasons.append(f"vol_ratio={vol_ratio:.2f}")
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=round(min(1.0, confidence), 2),
                reason="OI_LONG_BUILDUP: " + ", ".join(reasons),
                raw_signals={"oi_change_pct": oi_change_pct, "r15m": r15m, "pcr": pcr, "vol_ratio": vol_ratio},
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_ce_close,
            )

        if oi_change_pct > self._oi_change_threshold and r15m < -self._min_directional_r15m and pcr is not None and pcr <= self._pcr_bear_max:
            reasons = [f"oi_chg={oi_change_pct:.2%}", f"r15m={r15m:.4f}"]
            confidence += 0.05
            reasons.append(f"pcr={pcr:.2f}")
            if vol_ratio > self._min_vol_ratio:
                confidence += 0.05
                reasons.append(f"vol_ratio={vol_ratio:.2f}")
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=round(min(1.0, confidence), 2),
                reason="OI_SHORT_BUILDUP: " + ", ".join(reasons),
                raw_signals={"oi_change_pct": oi_change_pct, "r15m": r15m, "pcr": pcr, "vol_ratio": vol_ratio},
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_pe_close,
            )

        if oi_change_pct < -self._oi_change_threshold:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.SKIP,
                direction=Direction.AVOID,
                confidence=0.65,
                reason=f"OI_UNWINDING: oi_chg={oi_change_pct:.2%}",
                raw_signals={"oi_change_pct": oi_change_pct},
            )
        return None

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if position.bars_held < self._min_exit_hold_bars:
            return None
        oi_change = snap.fut_oi_change_30m
        fut_oi = snap.fut_oi
        # Exit uses r5m (not r15m) to react to immediate momentum reversal.
        r5m = snap.fut_return_5m
        if oi_change is None or fut_oi is None or fut_oi <= 0 or r5m is None:
            return None
        oi_change_pct = oi_change / fut_oi
        if (
            position.direction == "CE"
            and oi_change_pct < -self._oi_change_threshold
            and r5m < -abs(self._exit_r5m_threshold)
        ):
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.75,
                reason=(
                    f"OI_LONG_UNWIND: oi_chg={oi_change_pct:.2%} "
                    f"r5m={r5m:.4f} min_hold={self._min_exit_hold_bars}"
                ),
                raw_signals={
                    "oi_change_pct": oi_change_pct,
                    "fut_oi_change_30m": oi_change,
                    "fut_oi": fut_oi,
                    "r5m": r5m,
                    "bars_held": position.bars_held,
                },
                exit_reason=ExitReason.REGIME_SHIFT,
            )
        if (
            position.direction == "PE"
            and oi_change_pct < -self._oi_change_threshold
            and r5m > abs(self._exit_r5m_threshold)
        ):
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.75,
                reason=(
                    f"OI_SHORT_COVER: oi_chg={oi_change_pct:.2%} "
                    f"r5m={r5m:.4f} min_hold={self._min_exit_hold_bars}"
                ),
                raw_signals={
                    "oi_change_pct": oi_change_pct,
                    "fut_oi_change_30m": oi_change,
                    "fut_oi": fut_oi,
                    "r5m": r5m,
                    "bars_held": position.bars_held,
                },
                exit_reason=ExitReason.REGIME_SHIFT,
            )
        return None


class EMAcrossoverStrategy(BaseStrategy):
    """Exact EMA stack/alignment strategy using snapshot-provided EMA fields."""

    name = "EMA_CROSSOVER"

    def __init__(
        self,
        *,
        min_spread_pct: float = 0.0003,
        confidence_base: float = 0.65,
        ema_exit_min_bars_held: int = 2,
        ema_exit_min_spread_pct: float = 0.001,
    ) -> None:
        self._min_spread_pct = min_spread_pct
        self._confidence_base = confidence_base
        self._ema_exit_min_bars_held = max(0, int(ema_exit_min_bars_held))
        self._ema_exit_min_spread_pct = float(ema_exit_min_spread_pct)

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)

        if position is not None:
            return self._check_exit(snap, position)
        if not snap.is_valid_entry_phase or snap.minutes < 30:
            return None

        close = snap.fut_close
        ema_9 = snap.ema_9
        ema_21 = snap.ema_21
        ema_50 = snap.ema_50
        if close is None or ema_9 is None or ema_21 is None or ema_50 is None:
            return None

        confidence = self._confidence_base
        fast_spread = abs(ema_9 - ema_21) / close if close else 0.0
        slow_spread = abs(ema_21 - ema_50) / close if close else 0.0
        aligned_bull = ema_9 > ema_21 > ema_50 and close > ema_9
        aligned_bear = ema_9 < ema_21 < ema_50 and close < ema_9
        strong_alignment = min(fast_spread, slow_spread) >= self._min_spread_pct

        if aligned_bull and strong_alignment:
            confidence = min(1.0, confidence + min(0.20, 0.10 * (fast_spread / self._min_spread_pct)))
            if snap.pcr is not None and snap.pcr > 1.0:
                confidence = min(1.0, confidence + 0.05)
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=round(confidence, 2),
                reason=f"EMA_BULL: close={close:.0f} ema9={ema_9:.0f} ema21={ema_21:.0f} ema50={ema_50:.0f}",
                raw_signals={
                    "close": close,
                    "ema_9": ema_9,
                    "ema_21": ema_21,
                    "ema_50": ema_50,
                    "fast_spread": fast_spread,
                    "slow_spread": slow_spread,
                    "pcr": snap.pcr,
                },
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_ce_close,
            )
        if aligned_bear and strong_alignment:
            confidence = min(1.0, confidence + min(0.20, 0.10 * (fast_spread / self._min_spread_pct)))
            if snap.pcr is not None and snap.pcr < 1.0:
                confidence = min(1.0, confidence + 0.05)
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=round(confidence, 2),
                reason=f"EMA_BEAR: close={close:.0f} ema9={ema_9:.0f} ema21={ema_21:.0f} ema50={ema_50:.0f}",
                raw_signals={
                    "close": close,
                    "ema_9": ema_9,
                    "ema_21": ema_21,
                    "ema_50": ema_50,
                    "fast_spread": fast_spread,
                    "slow_spread": slow_spread,
                    "pcr": snap.pcr,
                },
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_pe_close,
            )
        return None

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        close = snap.fut_close
        ema_9 = snap.ema_9
        ema_21 = snap.ema_21
        if close is None or ema_9 is None or ema_21 is None:
            return None
        if position.bars_held < self._ema_exit_min_bars_held:
            return None
        spread_to_price = abs(ema_9 - ema_21) / close if close else 0.0
        if spread_to_price < self._ema_exit_min_spread_pct:
            return None
        if position.direction == "CE" and ema_9 < ema_21:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.65,
                reason=f"EMA_EXIT: close={close:.0f} ema9={ema_9:.0f} ema21={ema_21:.0f}",
                raw_signals={"bars_held": position.bars_held, "ema_spread": spread_to_price},
                exit_reason=ExitReason.REGIME_SHIFT,
            )
        if position.direction == "PE" and ema_9 > ema_21:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.65,
                reason=f"EMA_EXIT: close={close:.0f} ema9={ema_9:.0f} ema21={ema_21:.0f}",
                raw_signals={"bars_held": position.bars_held, "ema_spread": spread_to_price},
                exit_reason=ExitReason.REGIME_SHIFT,
            )
        return None


class VWAPReclaimStrategy(BaseStrategy):
    """VWAP reclaim/rejection using exact session VWAP from snapshot."""

    name = "VWAP_RECLAIM"

    def __init__(self, *, vol_ratio_min: float = 1.3, confidence_base: float = 0.65) -> None:
        self._vol_ratio_min = vol_ratio_min
        self._confidence_base = confidence_base

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)

        if position is not None:
            return self._check_exit(snap, position)
        if not snap.is_valid_entry_phase or snap.minutes < 45:
            return None

        close = snap.fut_close
        vwap = snap.vwap
        price_vs_vwap = snap.price_vs_vwap
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        vol_ratio = snap.vol_ratio
        if close is None or vwap is None or r5m is None or r15m is None or vol_ratio is None:
            return None

        confidence = self._confidence_base
        if close > vwap and r15m < -0.001 and r5m > 0.001 and vol_ratio >= self._vol_ratio_min:
            confidence = min(1.0, confidence + 0.10 * (vol_ratio - self._vol_ratio_min))
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=round(confidence, 2),
                reason=f"VWAP_RECLAIM: close={close:.0f}>vwap={vwap:.0f} r15m={r15m:.4f} r5m={r5m:.4f}",
                raw_signals={
                    "close": close,
                    "vwap": vwap,
                    "price_vs_vwap": price_vs_vwap,
                    "r5m": r5m,
                    "r15m": r15m,
                    "vol_ratio": vol_ratio,
                },
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_ce_close,
            )
        if close < vwap and r15m > 0.001 and r5m < -0.001 and vol_ratio >= self._vol_ratio_min:
            confidence = min(1.0, confidence + 0.10 * (vol_ratio - self._vol_ratio_min))
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=round(confidence, 2),
                reason=f"VWAP_REJECTION: close={close:.0f}<vwap={vwap:.0f} r15m={r15m:.4f} r5m={r5m:.4f}",
                raw_signals={
                    "close": close,
                    "vwap": vwap,
                    "price_vs_vwap": price_vs_vwap,
                    "r5m": r5m,
                    "r15m": r15m,
                    "vol_ratio": vol_ratio,
                },
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_pe_close,
            )
        return None

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        close = snap.fut_close
        vwap = snap.vwap
        if close is None or vwap is None:
            return None
        if position.direction == "CE" and close < vwap:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.70,
                reason=f"VWAP_RECLAIM_EXIT: close={close:.0f}<vwap={vwap:.0f}",
                exit_reason=ExitReason.REGIME_SHIFT,
            )
        if position.direction == "PE" and close > vwap:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.70,
                reason=f"VWAP_REJECTION_EXIT: close={close:.0f}>vwap={vwap:.0f}",
                exit_reason=ExitReason.REGIME_SHIFT,
            )
        return None


class ORBRetestContinuationStrategy(_TraderSetupStrategy):
    """Trade only after the OR break retests and re-accepts."""

    name = "ORB_RETEST_CONTINUATION"

    def __init__(
        self,
        *,
        breakout_buffer_pct: float = 0.0006,
        retest_tolerance_pct: float = 0.0008,
        invalidation_buffer_pct: float = 0.0012,
        resume_buffer_pct: float = 0.0007,
        breakout_r5m_min: float = 0.0008,
        resume_r5m_min: float = 0.0006,
        min_r15m_confirm: float = 0.0006,
        min_vol_ratio: float = 1.20,
        min_option_vol_ratio: float = 1.10,
        min_oi_change_pct: float = 0.003,
        pcr_bull_min: float = 0.95,
        pcr_bear_max: float = 1.05,
        max_entry_minute: int = 150,
        max_setup_age_minutes: int = 55,
        exit_buffer_pct: float = 0.0012,
    ) -> None:
        self._breakout_buffer_pct = float(breakout_buffer_pct)
        self._retest_tolerance_pct = float(retest_tolerance_pct)
        self._invalidation_buffer_pct = float(invalidation_buffer_pct)
        self._resume_buffer_pct = float(resume_buffer_pct)
        self._breakout_r5m_min = float(breakout_r5m_min)
        self._resume_r5m_min = float(resume_r5m_min)
        self._min_r15m_confirm = float(min_r15m_confirm)
        self._min_vol_ratio = float(min_vol_ratio)
        self._min_option_vol_ratio = float(min_option_vol_ratio)
        self._min_oi_change_pct = float(min_oi_change_pct)
        self._pcr_bull_min = float(pcr_bull_min)
        self._pcr_bear_max = float(pcr_bear_max)
        self._max_entry_minute = int(max_entry_minute)
        self._max_setup_age_minutes = int(max_setup_age_minutes)
        self._exit_buffer_pct = float(exit_buffer_pct)
        self._active_setup: Optional[dict[str, Any]] = None
        self._entry_fired = False
        self._trade_date: Optional[str] = None

    def on_session_start(self, trade_date) -> None:
        self._trade_date = str(trade_date)
        self._active_setup = None
        self._entry_fired = False

    def on_session_end(self, trade_date) -> None:
        self.on_session_start(trade_date)

    def _ensure_session(self, snap: SnapshotAccessor) -> None:
        if self._trade_date != snap.trade_date:
            self.on_session_start(snap.trade_date)

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        self._ensure_session(snap)

        if position is not None:
            return self._check_exit(snap, position)
        if self._entry_fired or not snap.is_valid_entry_phase or not snap.or_ready or snap.minutes > self._max_entry_minute:
            return None

        close = snap.fut_close
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        vol_ratio = snap.vol_ratio
        if close is None or r5m is None or r15m is None or vol_ratio is None:
            return None

        self._update_breakout_state(snap, close, r5m, vol_ratio)
        setup = self._active_setup
        if not setup:
            return None
        if snap.minutes - int(setup["breakout_minute"]) > self._max_setup_age_minutes:
            self._active_setup = None
            return None

        direction = setup["direction"]
        level = float(setup["level"])
        oi_change_pct = self._oi_change_pct(snap)
        if direction == Direction.CE:
            if close < level * (1.0 - self._invalidation_buffer_pct):
                self._active_setup = None
                return None
            if not setup["retest_seen"] and abs(close - level) / level <= self._retest_tolerance_pct and r5m > -self._resume_r5m_min:
                setup["retest_seen"] = True
                setup["retest_minute"] = snap.minutes
                return None
            if (
                setup["retest_seen"]
                and snap.minutes > int(setup.get("retest_minute") or setup["breakout_minute"])
                and close > level * (1.0 + self._resume_buffer_pct)
                and r5m >= self._resume_r5m_min
                and r15m >= self._min_r15m_confirm
                and vol_ratio >= self._min_vol_ratio
                and (snap.pcr is None or snap.pcr >= self._pcr_bull_min)
                and (self._option_liquidity_ratio(snap, Direction.CE) or 0.0) >= self._min_option_vol_ratio
                and self._oi_confirms(oi_change_pct, self._min_oi_change_pct)
            ):
                self._entry_fired = True
                return self._entry_vote(
                    snap,
                    direction=Direction.CE,
                    confidence=0.86,
                    reason=f"ORB_RETEST_LONG: break={level:.0f} r5m={r5m:.4f} r15m={r15m:.4f}",
                    raw_signals={
                        "level": level,
                        "breakout_minute": setup["breakout_minute"],
                        "retest_minute": setup.get("retest_minute"),
                        "close": close,
                        "r5m": r5m,
                        "r15m": r15m,
                        "vol_ratio": vol_ratio,
                        "pcr": snap.pcr,
                        "oi_change_pct": oi_change_pct,
                        "option_vol_ratio": self._option_liquidity_ratio(snap, Direction.CE),
                    },
                )
            return None

        if close > level * (1.0 + self._invalidation_buffer_pct):
            self._active_setup = None
            return None
        if not setup["retest_seen"] and abs(close - level) / level <= self._retest_tolerance_pct and r5m < self._resume_r5m_min:
            setup["retest_seen"] = True
            setup["retest_minute"] = snap.minutes
            return None
        if (
            setup["retest_seen"]
            and snap.minutes > int(setup.get("retest_minute") or setup["breakout_minute"])
            and close < level * (1.0 - self._resume_buffer_pct)
            and r5m <= -self._resume_r5m_min
            and r15m <= -self._min_r15m_confirm
            and vol_ratio >= self._min_vol_ratio
            and (snap.pcr is None or snap.pcr <= self._pcr_bear_max)
            and (self._option_liquidity_ratio(snap, Direction.PE) or 0.0) >= self._min_option_vol_ratio
            and self._oi_confirms(oi_change_pct, self._min_oi_change_pct)
        ):
            self._entry_fired = True
            return self._entry_vote(
                snap,
                direction=Direction.PE,
                confidence=0.86,
                reason=f"ORB_RETEST_SHORT: break={level:.0f} r5m={r5m:.4f} r15m={r15m:.4f}",
                raw_signals={
                    "level": level,
                    "breakout_minute": setup["breakout_minute"],
                    "retest_minute": setup.get("retest_minute"),
                    "close": close,
                    "r5m": r5m,
                    "r15m": r15m,
                    "vol_ratio": vol_ratio,
                    "pcr": snap.pcr,
                    "oi_change_pct": oi_change_pct,
                    "option_vol_ratio": self._option_liquidity_ratio(snap, Direction.PE),
                },
            )
        return None

    def _update_breakout_state(self, snap: SnapshotAccessor, close: float, r5m: float, vol_ratio: float) -> None:
        if self._active_setup is not None:
            return
        if vol_ratio < self._min_vol_ratio:
            return
        if snap.orh is not None and snap.orh_broken and close > snap.orh * (1.0 + self._breakout_buffer_pct) and r5m >= self._breakout_r5m_min:
            self._active_setup = {"direction": Direction.CE, "level": snap.orh, "breakout_minute": snap.minutes, "retest_seen": False}
            return
        if snap.orl is not None and snap.orl_broken and close < snap.orl * (1.0 - self._breakout_buffer_pct) and r5m <= -self._breakout_r5m_min:
            self._active_setup = {"direction": Direction.PE, "level": snap.orl, "breakout_minute": snap.minutes, "retest_seen": False}

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if not snap.or_ready or snap.fut_close is None or snap.orh is None or snap.orl is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.orh * (1.0 - self._exit_buffer_pct):
            return self._exit_vote(
                snap,
                confidence=0.82,
                reason=f"ORB_RETEST_EXIT_LONG: close={snap.fut_close:.0f}<orh={snap.orh:.0f}",
                raw_signals={"close": snap.fut_close, "orh": snap.orh},
            )
        if position.direction == "PE" and snap.fut_close > snap.orl * (1.0 + self._exit_buffer_pct):
            return self._exit_vote(
                snap,
                confidence=0.82,
                reason=f"ORB_RETEST_EXIT_SHORT: close={snap.fut_close:.0f}>orl={snap.orl:.0f}",
                raw_signals={"close": snap.fut_close, "orl": snap.orl},
            )
        return None


class VWAPPullbackContinuationStrategy(_TraderSetupStrategy):
    """Trade trend continuation only after a pullback toward VWAP."""

    name = "VWAP_PULLBACK_CONTINUATION"

    def __init__(
        self,
        *,
        min_entry_minute: int = 50,
        max_entry_minute: int = 210,
        trend_r15m_min: float = 0.0007,
        trend_r30m_min: float = 0.0012,
        pullback_distance_pct: float = 0.0018,
        resume_distance_pct: float = 0.0005,
        resume_r5m_min: float = 0.0006,
        min_vol_ratio: float = 1.15,
        min_option_vol_ratio: float = 1.05,
        min_oi_change_pct: float = 0.002,
        pcr_bull_min: float = 0.95,
        pcr_bear_max: float = 1.05,
        max_setup_age_minutes: int = 75,
        exit_buffer_pct: float = 0.0008,
    ) -> None:
        self._min_entry_minute = int(min_entry_minute)
        self._max_entry_minute = int(max_entry_minute)
        self._trend_r15m_min = float(trend_r15m_min)
        self._trend_r30m_min = float(trend_r30m_min)
        self._pullback_distance_pct = float(pullback_distance_pct)
        self._resume_distance_pct = float(resume_distance_pct)
        self._resume_r5m_min = float(resume_r5m_min)
        self._min_vol_ratio = float(min_vol_ratio)
        self._min_option_vol_ratio = float(min_option_vol_ratio)
        self._min_oi_change_pct = float(min_oi_change_pct)
        self._pcr_bull_min = float(pcr_bull_min)
        self._pcr_bear_max = float(pcr_bear_max)
        self._max_setup_age_minutes = int(max_setup_age_minutes)
        self._exit_buffer_pct = float(exit_buffer_pct)
        self._active_setup: Optional[dict[str, Any]] = None
        self._entry_fired = False
        self._trade_date: Optional[str] = None

    def on_session_start(self, trade_date) -> None:
        self._trade_date = str(trade_date)
        self._active_setup = None
        self._entry_fired = False

    def on_session_end(self, trade_date) -> None:
        self.on_session_start(trade_date)

    def _ensure_session(self, snap: SnapshotAccessor) -> None:
        if self._trade_date != snap.trade_date:
            self.on_session_start(snap.trade_date)

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        self._ensure_session(snap)

        if position is not None:
            return self._check_exit(snap, position)
        if self._entry_fired or not snap.is_valid_entry_phase or snap.minutes < self._min_entry_minute or snap.minutes > self._max_entry_minute:
            return None

        close = snap.fut_close
        vwap = snap.vwap
        price_vs_vwap = snap.price_vs_vwap
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        r30m = snap.fut_return_30m
        vol_ratio = snap.vol_ratio
        if None in (close, vwap, price_vs_vwap, r5m, r15m, r30m, vol_ratio):
            return None

        self._update_bias_state(snap, close, vwap, r15m, r30m, vol_ratio)
        setup = self._active_setup
        if not setup:
            return None
        if snap.minutes - int(setup["bias_minute"]) > self._max_setup_age_minutes:
            self._active_setup = None
            return None

        oi_change_pct = self._oi_change_pct(snap)
        if setup["direction"] == Direction.CE:
            if close < vwap * (1.0 - self._pullback_distance_pct):
                self._active_setup = None
                return None
            if not setup["pullback_seen"] and price_vs_vwap <= self._pullback_distance_pct and r5m <= 0.0:
                setup["pullback_seen"] = True
                setup["pullback_minute"] = snap.minutes
                return None
            if (
                setup["pullback_seen"]
                and close > vwap * (1.0 + self._resume_distance_pct)
                and r5m >= self._resume_r5m_min
                and r15m >= self._trend_r15m_min
                and vol_ratio >= self._min_vol_ratio
                and (snap.pcr is None or snap.pcr >= self._pcr_bull_min)
                and (self._option_liquidity_ratio(snap, Direction.CE) or 0.0) >= self._min_option_vol_ratio
                and self._oi_confirms(oi_change_pct, self._min_oi_change_pct)
            ):
                self._entry_fired = True
                return self._entry_vote(
                    snap,
                    direction=Direction.CE,
                    confidence=0.83,
                    reason=f"VWAP_PULLBACK_LONG: vwap={vwap:.0f} close={close:.0f} r5m={r5m:.4f}",
                    raw_signals={
                        "bias_minute": setup["bias_minute"],
                        "pullback_minute": setup.get("pullback_minute"),
                        "vwap": vwap,
                        "close": close,
                        "price_vs_vwap": price_vs_vwap,
                        "r5m": r5m,
                        "r15m": r15m,
                        "r30m": r30m,
                        "vol_ratio": vol_ratio,
                        "pcr": snap.pcr,
                        "oi_change_pct": oi_change_pct,
                        "option_vol_ratio": self._option_liquidity_ratio(snap, Direction.CE),
                    },
                )
            return None

        if close > vwap * (1.0 + self._pullback_distance_pct):
            self._active_setup = None
            return None
        if not setup["pullback_seen"] and price_vs_vwap >= -self._pullback_distance_pct and r5m >= 0.0:
            setup["pullback_seen"] = True
            setup["pullback_minute"] = snap.minutes
            return None
        if (
            setup["pullback_seen"]
            and close < vwap * (1.0 - self._resume_distance_pct)
            and r5m <= -self._resume_r5m_min
            and r15m <= -self._trend_r15m_min
            and vol_ratio >= self._min_vol_ratio
            and (snap.pcr is None or snap.pcr <= self._pcr_bear_max)
            and (self._option_liquidity_ratio(snap, Direction.PE) or 0.0) >= self._min_option_vol_ratio
            and self._oi_confirms(oi_change_pct, self._min_oi_change_pct)
        ):
            self._entry_fired = True
            return self._entry_vote(
                snap,
                direction=Direction.PE,
                confidence=0.83,
                reason=f"VWAP_PULLBACK_SHORT: vwap={vwap:.0f} close={close:.0f} r5m={r5m:.4f}",
                raw_signals={
                    "bias_minute": setup["bias_minute"],
                    "pullback_minute": setup.get("pullback_minute"),
                    "vwap": vwap,
                    "close": close,
                    "price_vs_vwap": price_vs_vwap,
                    "r5m": r5m,
                    "r15m": r15m,
                    "r30m": r30m,
                    "vol_ratio": vol_ratio,
                    "pcr": snap.pcr,
                    "oi_change_pct": oi_change_pct,
                    "option_vol_ratio": self._option_liquidity_ratio(snap, Direction.PE),
                },
            )
        return None

    def _update_bias_state(self, snap: SnapshotAccessor, close: float, vwap: float, r15m: float, r30m: float, vol_ratio: float) -> None:
        if self._active_setup is not None:
            return
        if vol_ratio < self._min_vol_ratio:
            return
        if close > vwap and r15m >= self._trend_r15m_min and r30m >= self._trend_r30m_min:
            self._active_setup = {"direction": Direction.CE, "bias_minute": snap.minutes, "pullback_seen": False}
            return
        if close < vwap and r15m <= -self._trend_r15m_min and r30m <= -self._trend_r30m_min:
            self._active_setup = {"direction": Direction.PE, "bias_minute": snap.minutes, "pullback_seen": False}

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if snap.fut_close is None or snap.vwap is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.vwap * (1.0 - self._exit_buffer_pct):
            return self._exit_vote(
                snap,
                confidence=0.78,
                reason=f"VWAP_PULLBACK_EXIT_LONG: close={snap.fut_close:.0f}<vwap={snap.vwap:.0f}",
                raw_signals={"close": snap.fut_close, "vwap": snap.vwap},
            )
        if position.direction == "PE" and snap.fut_close > snap.vwap * (1.0 + self._exit_buffer_pct):
            return self._exit_vote(
                snap,
                confidence=0.78,
                reason=f"VWAP_PULLBACK_EXIT_SHORT: close={snap.fut_close:.0f}>vwap={snap.vwap:.0f}",
                raw_signals={"close": snap.fut_close, "vwap": snap.vwap},
            )
        return None


class FailedBreakoutReversalStrategy(_TraderSetupStrategy):
    """Fade a failed OR break only after price re-enters structure."""

    name = "FAILED_BREAKOUT_REVERSAL"

    def __init__(
        self,
        *,
        min_entry_minute: int = 35,
        max_entry_minute: int = 160,
        breakout_buffer_pct: float = 0.0007,
        reentry_buffer_pct: float = 0.0002,
        continuation_invalidation_pct: float = 0.0018,
        reversal_r5m_min: float = 0.0006,
        min_vol_ratio: float = 1.10,
        min_option_vol_ratio: float = 1.05,
        pcr_bull_min: float = 0.93,
        pcr_bear_max: float = 1.07,
        max_setup_age_minutes: int = 45,
        exit_buffer_pct: float = 0.0012,
    ) -> None:
        self._min_entry_minute = int(min_entry_minute)
        self._max_entry_minute = int(max_entry_minute)
        self._breakout_buffer_pct = float(breakout_buffer_pct)
        self._reentry_buffer_pct = float(reentry_buffer_pct)
        self._continuation_invalidation_pct = float(continuation_invalidation_pct)
        self._reversal_r5m_min = float(reversal_r5m_min)
        self._min_vol_ratio = float(min_vol_ratio)
        self._min_option_vol_ratio = float(min_option_vol_ratio)
        self._pcr_bull_min = float(pcr_bull_min)
        self._pcr_bear_max = float(pcr_bear_max)
        self._max_setup_age_minutes = int(max_setup_age_minutes)
        self._exit_buffer_pct = float(exit_buffer_pct)
        self._failed_break: Optional[dict[str, Any]] = None
        self._entry_fired = False
        self._trade_date: Optional[str] = None

    def on_session_start(self, trade_date) -> None:
        self._trade_date = str(trade_date)
        self._failed_break = None
        self._entry_fired = False

    def on_session_end(self, trade_date) -> None:
        self.on_session_start(trade_date)

    def _ensure_session(self, snap: SnapshotAccessor) -> None:
        if self._trade_date != snap.trade_date:
            self.on_session_start(snap.trade_date)

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        self._ensure_session(snap)

        if position is not None:
            return self._check_exit(snap, position)
        if (
            self._entry_fired
            or not snap.is_valid_entry_phase
            or not snap.or_ready
            or snap.minutes < self._min_entry_minute
            or snap.minutes > self._max_entry_minute
        ):
            return None

        close = snap.fut_close
        r5m = snap.fut_return_5m
        vol_ratio = snap.vol_ratio
        if close is None or r5m is None or vol_ratio is None:
            return None

        self._update_failed_break_state(snap, close, r5m, vol_ratio)
        state = self._failed_break
        if not state:
            return None
        if snap.minutes - int(state["break_minute"]) > self._max_setup_age_minutes:
            self._failed_break = None
            return None

        level = float(state["level"])
        if state["failed_direction"] == "UP":
            if close > level * (1.0 + self._continuation_invalidation_pct):
                self._failed_break = None
                return None
            if (
                close < level * (1.0 - self._reentry_buffer_pct)
                and r5m <= -self._reversal_r5m_min
                and vol_ratio >= self._min_vol_ratio
                and (snap.pcr is None or snap.pcr <= self._pcr_bear_max)
                and (self._option_liquidity_ratio(snap, Direction.PE) or 0.0) >= self._min_option_vol_ratio
            ):
                self._entry_fired = True
                return self._entry_vote(
                    snap,
                    direction=Direction.PE,
                    confidence=0.80,
                    reason=f"FAILED_BREAKOUT_SHORT: reentered_below_orh={level:.0f} r5m={r5m:.4f}",
                    raw_signals={
                        "failed_break": "UP",
                        "break_minute": state["break_minute"],
                        "close": close,
                        "level": level,
                        "r5m": r5m,
                        "vol_ratio": vol_ratio,
                        "pcr": snap.pcr,
                        "option_vol_ratio": self._option_liquidity_ratio(snap, Direction.PE),
                    },
                )
            return None

        if close < level * (1.0 - self._continuation_invalidation_pct):
            self._failed_break = None
            return None
        if (
            close > level * (1.0 + self._reentry_buffer_pct)
            and r5m >= self._reversal_r5m_min
            and vol_ratio >= self._min_vol_ratio
            and (snap.pcr is None or snap.pcr >= self._pcr_bull_min)
            and (self._option_liquidity_ratio(snap, Direction.CE) or 0.0) >= self._min_option_vol_ratio
        ):
            self._entry_fired = True
            return self._entry_vote(
                snap,
                direction=Direction.CE,
                confidence=0.80,
                reason=f"FAILED_BREAKOUT_LONG: reentered_above_orl={level:.0f} r5m={r5m:.4f}",
                raw_signals={
                    "failed_break": "DOWN",
                    "break_minute": state["break_minute"],
                    "close": close,
                    "level": level,
                    "r5m": r5m,
                    "vol_ratio": vol_ratio,
                    "pcr": snap.pcr,
                    "option_vol_ratio": self._option_liquidity_ratio(snap, Direction.CE),
                },
            )
        return None

    def _update_failed_break_state(self, snap: SnapshotAccessor, close: float, r5m: float, vol_ratio: float) -> None:
        if self._failed_break is not None:
            return
        if vol_ratio < self._min_vol_ratio:
            return
        if snap.orh is not None and snap.orh_broken and close > snap.orh * (1.0 + self._breakout_buffer_pct) and r5m >= self._reversal_r5m_min:
            self._failed_break = {"failed_direction": "UP", "level": snap.orh, "break_minute": snap.minutes}
            return
        if snap.orl is not None and snap.orl_broken and close < snap.orl * (1.0 - self._breakout_buffer_pct) and r5m <= -self._reversal_r5m_min:
            self._failed_break = {"failed_direction": "DOWN", "level": snap.orl, "break_minute": snap.minutes}

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if snap.fut_close is None or snap.orh is None or snap.orl is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.orl * (1.0 - self._exit_buffer_pct):
            return self._exit_vote(
                snap,
                confidence=0.77,
                reason=f"FAILED_BREAKOUT_EXIT_LONG: close={snap.fut_close:.0f}<orl={snap.orl:.0f}",
                raw_signals={"close": snap.fut_close, "orl": snap.orl},
            )
        if position.direction == "PE" and snap.fut_close > snap.orh * (1.0 + self._exit_buffer_pct):
            return self._exit_vote(
                snap,
                confidence=0.77,
                reason=f"FAILED_BREAKOUT_EXIT_SHORT: close={snap.fut_close:.0f}>orh={snap.orh:.0f}",
                raw_signals={"close": snap.fut_close, "orh": snap.orh},
            )
        return None


class IVRegimeFilter(BaseStrategy):
    """Non-directional veto when premium is expensive or VIX is unstable."""

    name = "IV_FILTER"

    def __init__(
        self,
        *,
        avoid_states: tuple[str, ...] = ("SPIKING",),
        spike_vix_chg_pct: float = 15.0,
        elevated_iv_percentile: float = 80.0,
        elevated_vix_chg_max: float = 8.0,
        extreme_iv_percentile: float = 95.0,
    ) -> None:
        self._avoid_states = {item.upper() for item in avoid_states}
        self._spike_vix_chg_pct = float(spike_vix_chg_pct)
        self._elevated_iv_percentile = float(elevated_iv_percentile)
        self._elevated_vix_chg_max = float(elevated_vix_chg_max)
        self._extreme_iv_percentile = float(extreme_iv_percentile)

    def configure(self, payload: Optional[dict[str, object]]) -> None:
        if not isinstance(payload, dict):
            return
        avoid_states = payload.get("avoid_states")
        if isinstance(avoid_states, list):
            self._avoid_states = {str(item).strip().upper() for item in avoid_states if str(item).strip()}
        for key, attr in (
            ("spike_vix_chg_pct", "_spike_vix_chg_pct"),
            ("elevated_iv_percentile", "_elevated_iv_percentile"),
            ("elevated_vix_chg_max", "_elevated_vix_chg_max"),
            ("extreme_iv_percentile", "_extreme_iv_percentile"),
        ):
            if key not in payload:
                continue
            try:
                setattr(self, attr, float(payload[key]))
            except (TypeError, ValueError):
                continue

    def _iv_state(self, snap: SnapshotAccessor) -> str:
        if snap.vix_spike_flag:
            return "SPIKING"
        vix_chg = snap.vix_intraday_chg
        if vix_chg is not None and abs(vix_chg) >= self._spike_vix_chg_pct:
            return "SPIKING"
        iv_pct = snap.iv_percentile
        if (
            iv_pct is not None
            and iv_pct > self._elevated_iv_percentile
            and vix_chg is not None
            and abs(vix_chg) < self._elevated_vix_chg_max
        ):
            return "ELEVATED_STABLE"
        return "NORMAL"

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        if position is not None:
            return None
        state = self._iv_state(snap)
        if state in self._avoid_states:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.SKIP,
                direction=Direction.AVOID,
                confidence=0.95,
                reason=f"IV_FILTER: state={state}",
                raw_signals={
                    "iv_state": state,
                    "vix_spike_flag": snap.vix_spike_flag,
                    "vix_intraday_chg": snap.vix_intraday_chg,
                    "iv_percentile": snap.iv_percentile,
                },
            )
        if snap.iv_percentile is not None and snap.iv_percentile > self._extreme_iv_percentile:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.SKIP,
                direction=Direction.AVOID,
                confidence=0.90,
                reason=f"IV_FILTER: extreme_iv_percentile={snap.iv_percentile:.1f}>{self._extreme_iv_percentile:.1f}",
                raw_signals={"iv_state": state, "iv_percentile": snap.iv_percentile},
            )
        return None


class ExpiryMaxPainStrategy(BaseStrategy):
    """Expiry day mean reversion toward max pain."""

    name = "EXPIRY_MAX_PAIN"

    def __init__(
        self,
        *,
        min_distance_points: float = 150.0,
        min_minute: int = 105,
        confidence_base: float = 0.70,
        max_bars_in_trade: int = 30,
        enabled: bool = False,
        max_entries_per_expiry_day: int = 1,
        min_vol_ratio: float = 1.5,
    ) -> None:
        self._min_distance_points = min_distance_points
        self._min_minute = min_minute
        self._confidence_base = confidence_base
        self._max_bars_in_trade = max(1, int(max_bars_in_trade))
        self._enabled = bool(enabled)
        self._max_entries_per_expiry_day = max(1, int(max_entries_per_expiry_day))
        self._min_vol_ratio = float(min_vol_ratio)
        self._trade_day: Optional[str] = None
        self._trade_day_entries = 0

    @staticmethod
    def _resolve_trade_day(snap: SnapshotAccessor) -> Optional[str]:
        trade_day = snap.trade_date
        if isinstance(trade_day, str) and trade_day:
            return trade_day
        ts = snap.timestamp
        if ts is not None:
            return ts.date().isoformat()
        return None

    def _next_expiry_entry(self, snap: SnapshotAccessor) -> bool:
        if not self._enabled:
            return False
        if not snap.trade_date and snap.timestamp is None:
            return False
        trade_day = self._resolve_trade_day(snap)
        if trade_day is None:
            return False
        if self._trade_day != trade_day:
            self._trade_day = trade_day
            self._trade_day_entries = 0
        if self._trade_day_entries >= self._max_entries_per_expiry_day:
            return False
        return True

    def _mark_expiry_entry(self, snap: SnapshotAccessor) -> None:
        if not self._enabled:
            return
        if not snap.trade_date and snap.timestamp is None:
            return
        trade_day = self._resolve_trade_day(snap)
        if trade_day is None:
            return
        if self._trade_day != trade_day:
            self._trade_day = trade_day
            self._trade_day_entries = 0
        self._trade_day_entries += 1

    def _is_halted_entry(self, snap: SnapshotAccessor) -> bool:
        if not self._enabled:
            return True
        if snap.fut_close is None or snap.max_pain is None:
            return True
        if snap.vol_ratio is None or snap.vol_ratio < self._min_vol_ratio:
            return True
        return False

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        if not snap.is_expiry_day:
            return None
        if position is not None:
            return self._check_exit(snap, position)
        if self._is_halted_entry(snap):
            return None
        if not snap.is_valid_entry_phase or snap.minutes < self._min_minute:
            return None
        if not self._next_expiry_entry(snap):
            return None
        distance = snap.fut_close - snap.max_pain
        if abs(distance) < self._min_distance_points:
            return None
        self._mark_expiry_entry(snap)
        confidence = min(1.0, self._confidence_base + abs(distance) / 1000.0)
        if distance > 0:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=round(confidence, 2),
                reason=f"MAX_PAIN_GRAVITY: close={snap.fut_close:.0f}>max_pain={snap.max_pain}",
                raw_signals={"close": snap.fut_close, "max_pain": snap.max_pain, "distance": distance},
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_pe_close,
            )
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=Direction.CE,
            confidence=round(confidence, 2),
            reason=f"MAX_PAIN_GRAVITY: close={snap.fut_close:.0f}<max_pain={snap.max_pain}",
            raw_signals={"close": snap.fut_close, "max_pain": snap.max_pain, "distance": distance},
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=snap.atm_ce_close,
        )

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if snap.fut_close is None or snap.max_pain is None:
            return None
        distance = snap.fut_close - snap.max_pain
        if position.bars_held >= self._max_bars_in_trade:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.80,
                reason=f"MAX_PAIN_TIMEOUT: bars={position.bars_held} max={self._max_bars_in_trade}",
                raw_signals={"bars_held": position.bars_held, "max_bars_in_trade": self._max_bars_in_trade},
                exit_reason=ExitReason.TIME_STOP,
            )
        if position.direction == "CE" and distance >= -50:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.80,
                reason=f"MAX_PAIN_REACHED: close={snap.fut_close:.0f} max_pain={snap.max_pain}",
                exit_reason=ExitReason.TARGET_HIT,
            )
        if position.direction == "PE" and distance <= 50:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.EXIT,
                direction=Direction.EXIT,
                confidence=0.80,
                reason=f"MAX_PAIN_REACHED: close={snap.fut_close:.0f} max_pain={snap.max_pain}",
                exit_reason=ExitReason.TARGET_HIT,
            )
        return None


class PrevDayLevelBreakout(BaseStrategy):
    """Breakout through previous day high or low with elevated volume."""

    name = "PREV_DAY_LEVEL"

    def __init__(
        self,
        *,
        vol_ratio_min: float = 1.8,
        confidence_base: float = 0.72,
        max_entry_minute: int = 195,
        exit_reentry_buffer_pct: float = 0.0015,
        min_exit_hold_bars: int = 2,
    ) -> None:
        self._vol_ratio_min = vol_ratio_min
        self._confidence_base = confidence_base
        self._max_entry_minute = max_entry_minute
        self._exit_reentry_buffer_pct = max(0.0, float(exit_reentry_buffer_pct))
        self._min_exit_hold_bars = max(0, int(min_exit_hold_bars))

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        del risk
        snap = SnapshotAccessor(snapshot)
        if position is not None:
            return self._check_exit(snap, position)
        if not snap.is_valid_entry_phase or snap.minutes > self._max_entry_minute:
            return None
        if (
            snap.fut_close is None
            or snap.prev_day_high is None
            or snap.prev_day_low is None
            or snap.vol_ratio is None
            or snap.vol_ratio < self._vol_ratio_min
        ):
            return None

        confidence = self._confidence_base
        if snap.pcr is not None:
            if snap.pcr > 1.1 and snap.fut_close > snap.prev_day_high:
                confidence = min(1.0, confidence + 0.07)
            if snap.pcr < 0.9 and snap.fut_close < snap.prev_day_low:
                confidence = min(1.0, confidence + 0.07)

        if snap.fut_close > snap.prev_day_high:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=round(confidence, 2),
                reason=f"PDH_BREAK: close={snap.fut_close:.0f}>pdh={snap.prev_day_high:.0f} vol_ratio={snap.vol_ratio:.2f}",
                raw_signals={"close": snap.fut_close, "pdh": snap.prev_day_high, "pdl": snap.prev_day_low, "vol_ratio": snap.vol_ratio},
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_ce_close,
            )
        if snap.fut_close < snap.prev_day_low:
            return StrategyVote(
                strategy_name=self.name,
                snapshot_id=snap.snapshot_id,
                timestamp=snap.timestamp_or_now,
                trade_date=snap.trade_date,
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=round(confidence, 2),
                reason=f"PDL_BREAK: close={snap.fut_close:.0f}<pdl={snap.prev_day_low:.0f} vol_ratio={snap.vol_ratio:.2f}",
                raw_signals={"close": snap.fut_close, "pdh": snap.prev_day_high, "pdl": snap.prev_day_low, "vol_ratio": snap.vol_ratio},
                proposed_strike=snap.atm_strike,
                proposed_entry_premium=snap.atm_pe_close,
            )
        return None

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if position.bars_held < self._min_exit_hold_bars or snap.fut_close is None:
            return None
        if position.direction == "CE" and snap.prev_day_high is not None:
            reclaim_level = snap.prev_day_high * (1.0 - self._exit_reentry_buffer_pct)
            if snap.fut_close < reclaim_level:
                return StrategyVote(
                    strategy_name=self.name,
                    snapshot_id=snap.snapshot_id,
                    timestamp=snap.timestamp_or_now,
                    trade_date=snap.trade_date,
                    signal_type=SignalType.EXIT,
                    direction=Direction.EXIT,
                    confidence=0.78,
                    reason=(
                        f"PDH_REENTRY: close={snap.fut_close:.0f}<pdh_buffer={reclaim_level:.0f} "
                        f"min_hold={self._min_exit_hold_bars}"
                    ),
                    raw_signals={
                        "close": snap.fut_close,
                        "pdh": snap.prev_day_high,
                        "pdl": snap.prev_day_low,
                        "exit_reentry_buffer_pct": self._exit_reentry_buffer_pct,
                        "bars_held": position.bars_held,
                    },
                    exit_reason=ExitReason.STRATEGY_EXIT,
                )
        if position.direction == "PE" and snap.prev_day_low is not None:
            reclaim_level = snap.prev_day_low * (1.0 + self._exit_reentry_buffer_pct)
            if snap.fut_close > reclaim_level:
                return StrategyVote(
                    strategy_name=self.name,
                    snapshot_id=snap.snapshot_id,
                    timestamp=snap.timestamp_or_now,
                    trade_date=snap.trade_date,
                    signal_type=SignalType.EXIT,
                    direction=Direction.EXIT,
                    confidence=0.78,
                    reason=(
                        f"PDL_REENTRY: close={snap.fut_close:.0f}>pdl_buffer={reclaim_level:.0f} "
                        f"min_hold={self._min_exit_hold_bars}"
                    ),
                    raw_signals={
                        "close": snap.fut_close,
                        "pdh": snap.prev_day_high,
                        "pdl": snap.prev_day_low,
                        "exit_reentry_buffer_pct": self._exit_reentry_buffer_pct,
                        "bars_held": position.bars_held,
                    },
                    exit_reason=ExitReason.STRATEGY_EXIT,
                )
        return None


def build_default_strategy_set() -> list[BaseStrategy]:
    """Default registry order for the deterministic rule engine."""

    return [
        IVRegimeFilter(),
        ORBStrategy(),
        OIBuildupStrategy(),
        EMAcrossoverStrategy(),
        VWAPReclaimStrategy(),
        PrevDayLevelBreakout(),
        TraderCompositeStrategy(),
        TraderV3CompositeStrategy(),
    ]

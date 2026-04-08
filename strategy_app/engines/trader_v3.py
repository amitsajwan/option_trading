"""Trader-grade intraday options composite for deterministic V3."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..contracts import (
    BaseStrategy,
    Direction,
    ExitReason,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyVote,
)
from .options_state import OptionSideState, OptionsState, OptionsStateBuilder
from .snapshot_accessor import SnapshotAccessor
from .trader_judgement import TraderAction, TraderAnnotationRecord
from .trader_regime_v3 import TraderRegimeClassifierV3, TraderRegimeV3, TraderRegimeV3Label


class TraderV3Playbook(str, Enum):
    TREND_PULLBACK_LONG = "TREND_PULLBACK_LONG"
    TREND_PULLBACK_SHORT = "TREND_PULLBACK_SHORT"
    FAILED_BREAKOUT_REVERSAL_LONG = "FAILED_BREAKOUT_REVERSAL_LONG"
    FAILED_BREAKOUT_REVERSAL_SHORT = "FAILED_BREAKOUT_REVERSAL_SHORT"
    EXPIRY_MOMENTUM_BREAK = "EXPIRY_MOMENTUM_BREAK"
    EXPIRY_PIN_REVERSAL = "EXPIRY_PIN_REVERSAL"


@dataclass(frozen=True)
class PlaybookSignal:
    playbook: TraderV3Playbook
    direction: Direction
    score: float
    expected_move_pct: float
    invalidation_reference: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class StrikeSelection:
    strike: int
    premium: float
    target_delta_band: str
    selected_strike_reason: str
    option_score: float
    side_delta: Optional[float]


@dataclass
class TrendPullbackState:
    direction: Direction
    bias_minute: int
    pullback_seen: bool = False


@dataclass
class FailedBreakoutState:
    failed_direction: str
    level: float
    break_minute: int


@dataclass
class ExpiryMomentumState:
    direction: Direction
    breakout_minute: int


@dataclass
class TraderV3SessionState:
    trade_date: Optional[str] = None
    entries_taken: int = 0
    playbook_entries: dict[str, int] = field(default_factory=dict)
    bad_session_lockout: bool = False
    trend_pullback: Optional[TrendPullbackState] = None
    failed_breakout: Optional[FailedBreakoutState] = None
    expiry_momentum: Optional[ExpiryMomentumState] = None


@dataclass(frozen=True)
class GovernorDecisionV3:
    allowed: bool
    reason: str


class StrikeSelectorV3:
    def select(
        self,
        *,
        options_state: OptionsState,
        direction: Direction,
        playbook: TraderV3Playbook,
        expected_move_pct: float,
    ) -> Optional[StrikeSelection]:
        band_min, band_max, premium_min, premium_max = self._band_config(playbook)
        candidates = options_state.side_candidates(direction)
        eligible = [
            row for row in candidates
            if row.premium is not None
            and row.premium > 0
            and premium_min <= row.premium <= premium_max
            and (row.volume is None or row.volume >= 5000.0)
            and (row.oi is None or row.oi >= 10000.0)
        ]
        if not eligible:
            return None

        band_text = f"{band_min:.2f}-{band_max:.2f}"
        scored: list[tuple[float, OptionSideState, str]] = []
        fallback: list[tuple[float, OptionSideState, str]] = []
        for candidate in eligible:
            option_score = self._option_score(candidate, expected_move_pct)
            if candidate.delta is not None:
                abs_delta = abs(candidate.delta)
                midpoint = (band_min + band_max) / 2.0
                if band_min <= abs_delta <= band_max:
                    closeness = 1.0 - abs(abs_delta - midpoint)
                    scored.append((option_score + closeness, candidate, "delta_band"))
                else:
                    fallback.append((option_score - abs(abs_delta - midpoint), candidate, "delta_fallback"))
            else:
                distance_bonus = max(0.0, 0.6 - (0.15 * candidate.distance_steps))
                fallback.append((option_score + distance_bonus, candidate, "moneyness_fallback"))
        ranked = scored or fallback
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        _, selected, reason = ranked[0]
        return StrikeSelection(
            strike=selected.strike,
            premium=float(selected.premium or 0.0),
            target_delta_band=band_text,
            selected_strike_reason=reason,
            option_score=round(self._option_score(selected, expected_move_pct), 3),
            side_delta=selected.delta,
        )

    def _band_config(self, playbook: TraderV3Playbook) -> tuple[float, float, float, float]:
        if playbook in {TraderV3Playbook.TREND_PULLBACK_LONG, TraderV3Playbook.TREND_PULLBACK_SHORT}:
            return 0.45, 0.65, 55.0, 260.0
        if playbook in {
            TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_LONG,
            TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_SHORT,
        }:
            return 0.35, 0.55, 40.0, 180.0
        if playbook == TraderV3Playbook.EXPIRY_MOMENTUM_BREAK:
            return 0.50, 0.70, 35.0, 160.0
        return 0.30, 0.50, 25.0, 120.0

    def _option_score(self, candidate: OptionSideState, expected_move_pct: float) -> float:
        score = 0.45
        premium = float(candidate.premium or 0.0)
        volume = float(candidate.volume or 0.0)
        oi = float(candidate.oi or 0.0)
        if premium > 0:
            score += min(0.15, 180.0 / max(premium, 1.0) * 0.03)
        score += min(0.20, volume / 100000.0)
        score += min(0.10, oi / 200000.0)
        score += min(0.10, expected_move_pct / 0.0030 * 0.08)
        return min(score, 0.95)


class TradeGovernorV3:
    def evaluate(
        self,
        *,
        snap: SnapshotAccessor,
        regime: TraderRegimeV3,
        playbook_signal: PlaybookSignal,
        strike_selection: Optional[StrikeSelection],
        state: TraderV3SessionState,
    ) -> GovernorDecisionV3:
        if regime.label == TraderRegimeV3Label.NO_TRADE:
            return GovernorDecisionV3(False, "no_trade_regime")
        if state.bad_session_lockout:
            return GovernorDecisionV3(False, "bad_session_lockout")
        if 135 <= snap.minutes <= 255 and regime.label not in {
            TraderRegimeV3Label.EXPIRY_MOMENTUM,
            TraderRegimeV3Label.EXPIRY_PINNING,
        }:
            return GovernorDecisionV3(False, "midday_no_trade")
        max_entries = 1 if regime.label in {TraderRegimeV3Label.EXPIRY_MOMENTUM, TraderRegimeV3Label.EXPIRY_PINNING} else 2
        if state.entries_taken >= max_entries:
            return GovernorDecisionV3(False, "session_entry_cap")
        playbook_count = state.playbook_entries.get(playbook_signal.playbook.value, 0)
        playbook_cap = 1 if "REVERSAL" in playbook_signal.playbook.value or "EXPIRY" in playbook_signal.playbook.value else 2
        if playbook_count >= playbook_cap:
            return GovernorDecisionV3(False, "playbook_entry_cap")
        if strike_selection is None:
            return GovernorDecisionV3(False, "no_acceptable_contract")
        if playbook_signal.score < 0.72:
            return GovernorDecisionV3(False, "playbook_score_too_low")
        if strike_selection.option_score < 0.58:
            return GovernorDecisionV3(False, "tradability_score_too_low")
        if playbook_signal.expected_move_pct < 0.0015:
            return GovernorDecisionV3(False, "expected_move_too_small")
        if regime.label == TraderRegimeV3Label.RANGE and "TREND_PULLBACK" in playbook_signal.playbook.value:
            return GovernorDecisionV3(False, "range_blocks_trend_pullback")
        return GovernorDecisionV3(True, "playbook_allowed")


class TraderV3CompositeStrategy(BaseStrategy):
    """Trader-grade deterministic intraday options composite."""

    name = "TRADER_V3_COMPOSITE"

    def __init__(self) -> None:
        self._options_builder = OptionsStateBuilder()
        self._regime_classifier = TraderRegimeClassifierV3()
        self._selector = StrikeSelectorV3()
        self._governor = TradeGovernorV3()
        self._state = TraderV3SessionState()

    def on_session_start(self, trade_date) -> None:
        self._state = TraderV3SessionState(trade_date=str(trade_date))

    def on_session_end(self, trade_date) -> None:
        self.on_session_start(trade_date)

    def _ensure_session(self, snap: SnapshotAccessor) -> None:
        if self._state.trade_date != snap.trade_date:
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

        options_state = self._options_builder.build(snap)
        regime = self._regime_classifier.assess(snap, options_state)
        if regime.label == TraderRegimeV3Label.NO_TRADE:
            return self._skip_vote(snap, regime=regime, reason="no_trade_regime", skip_reason="no_trade_regime")

        self._observe_states(snap, regime)
        playbook = self._select_playbook(snap, regime)
        if playbook is None:
            return None
        strike = self._selector.select(
            options_state=options_state,
            direction=playbook.direction,
            playbook=playbook.playbook,
            expected_move_pct=playbook.expected_move_pct,
        )
        governor = self._governor.evaluate(
            snap=snap,
            regime=regime,
            playbook_signal=playbook,
            strike_selection=strike,
            state=self._state,
        )
        if not governor.allowed:
            return self._skip_vote(
                snap,
                regime=regime,
                reason=governor.reason,
                skip_reason=governor.reason,
                playbook=playbook,
                strike=strike,
            )
        if strike is None:
            return self._skip_vote(
                snap,
                regime=regime,
                reason="no_acceptable_contract",
                skip_reason="no_acceptable_contract",
                playbook=playbook,
            )

        self._state.entries_taken += 1
        self._state.playbook_entries[playbook.playbook.value] = self._state.playbook_entries.get(playbook.playbook.value, 0) + 1
        self._reset_setup_state()

        confidence = min(0.95, (0.40 * regime.score) + (0.40 * playbook.score) + (0.20 * strike.option_score))
        annotation = TraderAnnotationRecord(
            snapshot_id=snap.snapshot_id,
            trade_date=snap.trade_date,
            day_type=regime.label.value,
            setup_type=playbook.playbook.value,
            action=TraderAction.TAKE.value,
            direction=playbook.direction.value,
            option_plan=f"delta_band={strike.target_delta_band}",
            invalidation_reference=playbook.invalidation_reference,
            notes="det_v3_v1",
        )
        stop_loss_pct, target_pct = self._risk_profile(playbook.playbook)
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=playbook.direction,
            confidence=round(confidence, 2),
            reason=(
                f"TRADER_V3_ENTRY: playbook={playbook.playbook.value} "
                f"regime={regime.label.value} invalidation={playbook.invalidation_reference}"
            ),
            raw_signals={
                "trader_regime_v3": regime.label.value,
                "playbook": playbook.playbook.value,
                "playbook_score": round(playbook.score, 3),
                "tradability_score": strike.option_score,
                "governor_reason": governor.reason,
                "expected_move_pct": round(playbook.expected_move_pct, 6),
                "target_delta_band": strike.target_delta_band,
                "selected_strike_reason": strike.selected_strike_reason,
                "invalidation_reference": playbook.invalidation_reference,
                "skip_reason": "",
                "annotation": annotation.to_payload(),
                "_lock_strike_selection": True,
                "_entry_policy_mode": "bypass",
                "_selected_delta": strike.side_delta,
            },
            proposed_strike=strike.strike,
            proposed_entry_premium=strike.premium,
            proposed_stop_loss_pct=stop_loss_pct,
            proposed_target_pct=target_pct,
        )

    def _observe_states(self, snap: SnapshotAccessor, regime: TraderRegimeV3) -> None:
        close = snap.fut_close
        vwap = snap.vwap
        r5m = snap.fut_return_5m or 0.0
        vol_ratio = snap.vol_ratio or 0.0
        if close is None:
            return

        if regime.label in {TraderRegimeV3Label.TREND_UP, TraderRegimeV3Label.VOL_EXPANSION} and regime.bias == Direction.CE and close > (vwap or close):
            if self._state.trend_pullback is None:
                self._state.trend_pullback = TrendPullbackState(Direction.CE, snap.minutes)
        if self._state.trend_pullback is not None and self._state.trend_pullback.direction == Direction.CE:
            if not self._state.trend_pullback.pullback_seen and (snap.price_vs_vwap or 0.0) <= 0.0015 and r5m <= 0.0:
                self._state.trend_pullback.pullback_seen = True
        elif regime.label in {TraderRegimeV3Label.TREND_DOWN, TraderRegimeV3Label.VOL_EXPANSION} and regime.bias == Direction.PE and close < (vwap or close):
            if self._state.trend_pullback is None:
                self._state.trend_pullback = TrendPullbackState(Direction.PE, snap.minutes)
        if self._state.trend_pullback is not None and self._state.trend_pullback.direction == Direction.PE:
            if not self._state.trend_pullback.pullback_seen and (snap.price_vs_vwap or 0.0) >= -0.0015 and r5m >= 0.0:
                self._state.trend_pullback.pullback_seen = True

        if self._state.failed_breakout is None and snap.or_ready and vol_ratio >= 1.15 and snap.orh is not None and snap.orl is not None:
            if snap.orh_broken and close > snap.orh * 1.0005:
                self._state.failed_breakout = FailedBreakoutState("UP", snap.orh, snap.minutes)
            elif snap.orl_broken and close < snap.orl * 0.9995:
                self._state.failed_breakout = FailedBreakoutState("DOWN", snap.orl, snap.minutes)

        if snap.is_expiry_day and regime.label == TraderRegimeV3Label.EXPIRY_MOMENTUM and regime.bias is not None and self._state.expiry_momentum is None:
            self._state.expiry_momentum = ExpiryMomentumState(regime.bias, snap.minutes)

    def _select_playbook(self, snap: SnapshotAccessor, regime: TraderRegimeV3) -> Optional[PlaybookSignal]:
        candidates: list[PlaybookSignal] = []
        for candidate in (
            self._trend_pullback_candidate(snap, regime),
            self._failed_breakout_candidate(snap),
            self._expiry_momentum_candidate(snap, regime),
            self._expiry_pin_candidate(snap, regime),
        ):
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            return None
        priority = {
            TraderV3Playbook.TREND_PULLBACK_LONG: 0,
            TraderV3Playbook.TREND_PULLBACK_SHORT: 0,
            TraderV3Playbook.EXPIRY_MOMENTUM_BREAK: 0,
            TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_LONG: 1,
            TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_SHORT: 1,
            TraderV3Playbook.EXPIRY_PIN_REVERSAL: 1,
        }
        candidates.sort(key=lambda item: (priority[item.playbook], -item.score))
        return candidates[0]

    def _trend_pullback_candidate(self, snap: SnapshotAccessor, regime: TraderRegimeV3) -> Optional[PlaybookSignal]:
        state = self._state.trend_pullback
        close = snap.fut_close
        vwap = snap.vwap
        r5m = snap.fut_return_5m or 0.0
        r15m = snap.fut_return_15m or 0.0
        if state is None or close is None or vwap is None:
            return None
        if snap.minutes - state.bias_minute > 90:
            self._state.trend_pullback = None
            return None
        if state.direction == Direction.CE and regime.label in {TraderRegimeV3Label.TREND_UP, TraderRegimeV3Label.VOL_EXPANSION} and state.pullback_seen:
            if close > vwap * 1.0004 and r5m >= 0.0005 and r15m >= 0.0010:
                return PlaybookSignal(
                    TraderV3Playbook.TREND_PULLBACK_LONG,
                    Direction.CE,
                    0.84,
                    max(abs(r15m) * 1.5, 0.0020),
                    "VWAP",
                    ("trend_pullback_resume_long",),
                )
        if state.direction == Direction.PE and regime.label in {TraderRegimeV3Label.TREND_DOWN, TraderRegimeV3Label.VOL_EXPANSION} and state.pullback_seen:
            if close < vwap * 0.9996 and r5m <= -0.0005 and r15m <= -0.0010:
                return PlaybookSignal(
                    TraderV3Playbook.TREND_PULLBACK_SHORT,
                    Direction.PE,
                    0.84,
                    max(abs(r15m) * 1.5, 0.0020),
                    "VWAP",
                    ("trend_pullback_resume_short",),
                )
        return None

    def _failed_breakout_candidate(self, snap: SnapshotAccessor) -> Optional[PlaybookSignal]:
        state = self._state.failed_breakout
        close = snap.fut_close
        r5m = snap.fut_return_5m or 0.0
        if state is None or close is None:
            return None
        if snap.minutes - state.break_minute > 45:
            self._state.failed_breakout = None
            return None
        if state.failed_direction == "UP" and close < state.level * 0.9998 and r5m <= -0.0006:
            return PlaybookSignal(
                TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_SHORT,
                Direction.PE,
                0.79,
                max(abs(r5m) * 2.0, 0.0018),
                "ORH",
                ("failed_breakout_short",),
            )
        if state.failed_direction == "DOWN" and close > state.level * 1.0002 and r5m >= 0.0006:
            return PlaybookSignal(
                TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_LONG,
                Direction.CE,
                0.79,
                max(abs(r5m) * 2.0, 0.0018),
                "ORL",
                ("failed_breakout_long",),
            )
        return None

    def _expiry_momentum_candidate(self, snap: SnapshotAccessor, regime: TraderRegimeV3) -> Optional[PlaybookSignal]:
        state = self._state.expiry_momentum
        close = snap.fut_close
        vwap = snap.vwap
        r5m = snap.fut_return_5m or 0.0
        if state is None or regime.label != TraderRegimeV3Label.EXPIRY_MOMENTUM or close is None or vwap is None:
            return None
        if snap.minutes - state.breakout_minute > 40:
            self._state.expiry_momentum = None
            return None
        if state.direction == Direction.CE and close > vwap * 1.0006 and r5m >= 0.0008:
            return PlaybookSignal(
                TraderV3Playbook.EXPIRY_MOMENTUM_BREAK,
                Direction.CE,
                0.85,
                max(abs(snap.fut_return_15m or 0.0) * 1.7, 0.0024),
                "VWAP",
                ("expiry_momentum_long",),
            )
        if state.direction == Direction.PE and close < vwap * 0.9994 and r5m <= -0.0008:
            return PlaybookSignal(
                TraderV3Playbook.EXPIRY_MOMENTUM_BREAK,
                Direction.PE,
                0.85,
                max(abs(snap.fut_return_15m or 0.0) * 1.7, 0.0024),
                "VWAP",
                ("expiry_momentum_short",),
            )
        return None

    def _expiry_pin_candidate(self, snap: SnapshotAccessor, regime: TraderRegimeV3) -> Optional[PlaybookSignal]:
        close = snap.fut_close
        max_pain = snap.max_pain
        price_vs_vwap = snap.price_vs_vwap or 0.0
        r5m = snap.fut_return_5m or 0.0
        if regime.label != TraderRegimeV3Label.EXPIRY_PINNING or close is None or max_pain is None or close <= 0:
            return None
        gap = close - max_pain
        if gap > 25 and price_vs_vwap > 0.0006 and r5m <= -0.0003:
            return PlaybookSignal(
                TraderV3Playbook.EXPIRY_PIN_REVERSAL,
                Direction.PE,
                0.80,
                0.0018,
                "MAX_PAIN",
                ("expiry_pin_reversal_short",),
            )
        if gap < -25 and price_vs_vwap < -0.0006 and r5m >= 0.0003:
            return PlaybookSignal(
                TraderV3Playbook.EXPIRY_PIN_REVERSAL,
                Direction.CE,
                0.80,
                0.0018,
                "MAX_PAIN",
                ("expiry_pin_reversal_long",),
            )
        return None

    def _skip_vote(
        self,
        snap: SnapshotAccessor,
        *,
        regime: TraderRegimeV3,
        reason: str,
        skip_reason: str,
        playbook: Optional[PlaybookSignal] = None,
        strike: Optional[StrikeSelection] = None,
    ) -> StrategyVote:
        annotation = TraderAnnotationRecord(
            snapshot_id=snap.snapshot_id,
            trade_date=snap.trade_date,
            day_type=regime.label.value,
            setup_type=(playbook.playbook.value if playbook is not None else "NONE"),
            action=TraderAction.SKIP.value,
            direction=(playbook.direction.value if playbook is not None else None),
            option_plan=(f"delta_band={strike.target_delta_band}" if strike is not None else ""),
            invalidation_reference=(playbook.invalidation_reference if playbook is not None else ""),
            notes=f"skip={skip_reason}",
        )
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.SKIP,
            direction=Direction.AVOID,
            confidence=round(max(regime.score, playbook.score if playbook is not None else 0.0), 2),
            reason=f"TRADER_V3_SKIP: {reason}",
            raw_signals={
                "trader_regime_v3": regime.label.value,
                "playbook": playbook.playbook.value if playbook is not None else "",
                "playbook_score": round(playbook.score, 3) if playbook is not None else 0.0,
                "tradability_score": strike.option_score if strike is not None else 0.0,
                "governor_reason": reason,
                "expected_move_pct": round(playbook.expected_move_pct, 6) if playbook is not None else 0.0,
                "target_delta_band": strike.target_delta_band if strike is not None else "",
                "selected_strike_reason": strike.selected_strike_reason if strike is not None else "",
                "invalidation_reference": playbook.invalidation_reference if playbook is not None else "",
                "skip_reason": skip_reason,
                "annotation": annotation.to_payload(),
            },
        )

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        playbook = self._playbook_from_position(position)
        if playbook is None:
            return None
        close = snap.fut_close
        vwap = snap.vwap
        r5m = snap.fut_return_5m or 0.0
        if close is None:
            return None

        vote: Optional[StrategyVote] = None
        if playbook == TraderV3Playbook.TREND_PULLBACK_LONG and vwap is not None and (close < vwap * 0.9994 or r5m <= -0.0008):
            vote = self._exit_vote(snap, "TREND_PULLBACK_LONG_EXIT", ExitReason.STRATEGY_EXIT)
        elif playbook == TraderV3Playbook.TREND_PULLBACK_SHORT and vwap is not None and (close > vwap * 1.0006 or r5m >= 0.0008):
            vote = self._exit_vote(snap, "TREND_PULLBACK_SHORT_EXIT", ExitReason.STRATEGY_EXIT)
        elif playbook == TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_LONG and snap.orl is not None and close < snap.orl * 0.9996:
            vote = self._exit_vote(snap, "FAILED_BREAKOUT_LONG_EXIT", ExitReason.STRATEGY_EXIT)
        elif playbook == TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_SHORT and snap.orh is not None and close > snap.orh * 1.0004:
            vote = self._exit_vote(snap, "FAILED_BREAKOUT_SHORT_EXIT", ExitReason.STRATEGY_EXIT)
        elif playbook == TraderV3Playbook.EXPIRY_MOMENTUM_BREAK and vwap is not None:
            if position.direction == "CE" and close < vwap * 0.9996:
                vote = self._exit_vote(snap, "EXPIRY_MOMENTUM_LONG_EXIT", ExitReason.STRATEGY_EXIT)
            elif position.direction == "PE" and close > vwap * 1.0004:
                vote = self._exit_vote(snap, "EXPIRY_MOMENTUM_SHORT_EXIT", ExitReason.STRATEGY_EXIT)
        elif playbook == TraderV3Playbook.EXPIRY_PIN_REVERSAL and snap.max_pain is not None:
            if abs(close - snap.max_pain) <= 15:
                vote = self._exit_vote(snap, "EXPIRY_PIN_TARGET", ExitReason.TARGET_HIT)

        if vote is not None and position.pnl_pct < 0:
            self._state.bad_session_lockout = True
        return vote

    def _exit_vote(self, snap: SnapshotAccessor, reason: str, exit_reason: ExitReason) -> StrategyVote:
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.80,
            reason=reason,
            raw_signals={},
            exit_reason=exit_reason,
        )

    def _risk_profile(self, playbook: TraderV3Playbook) -> tuple[float, float]:
        if playbook in {TraderV3Playbook.TREND_PULLBACK_LONG, TraderV3Playbook.TREND_PULLBACK_SHORT}:
            return 0.18, 0.65
        if playbook in {
            TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_LONG,
            TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_SHORT,
        }:
            return 0.16, 0.50
        if playbook == TraderV3Playbook.EXPIRY_MOMENTUM_BREAK:
            return 0.15, 0.45
        return 0.12, 0.35

    def _reset_setup_state(self) -> None:
        self._state.trend_pullback = None
        self._state.failed_breakout = None
        self._state.expiry_momentum = None

    def _playbook_from_position(self, position: PositionContext) -> Optional[TraderV3Playbook]:
        reason = str(position.entry_reason or "")
        for item in TraderV3Playbook:
            if f"playbook={item.value}" in reason:
                return item
        return None

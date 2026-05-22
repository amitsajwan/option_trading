"""Trader-style market interpretation and annotation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from ..contracts import Direction
from .snapshot_accessor import SnapshotAccessor


class TraderDayType(str, Enum):
    TREND = "TREND"
    REVERSAL = "REVERSAL"
    BALANCED = "BALANCED"
    NO_TRADE = "NO_TRADE"


class TraderSetupType(str, Enum):
    NONE = "NONE"
    ORB_RETEST = "ORB_RETEST"
    VWAP_PULLBACK = "VWAP_PULLBACK"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"


class TraderAction(str, Enum):
    TAKE = "TAKE"
    SKIP = "SKIP"
    WATCH = "WATCH"


@dataclass(frozen=True)
class TraderAnnotationRecord:
    snapshot_id: str
    trade_date: str
    day_type: str
    setup_type: str
    action: str
    direction: Optional[str] = None
    option_plan: str = ""
    invalidation_reference: str = ""
    notes: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "trade_date": self.trade_date,
            "day_type": self.day_type,
            "setup_type": self.setup_type,
            "action": self.action,
            "direction": self.direction,
            "option_plan": self.option_plan,
            "invalidation_reference": self.invalidation_reference,
            "notes": self.notes,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TraderAnnotationRecord":
        return cls(
            snapshot_id=str(payload.get("snapshot_id") or ""),
            trade_date=str(payload.get("trade_date") or ""),
            day_type=str(payload.get("day_type") or TraderDayType.BALANCED.value),
            setup_type=str(payload.get("setup_type") or TraderSetupType.NONE.value),
            action=str(payload.get("action") or TraderAction.WATCH.value),
            direction=(str(payload["direction"]) if payload.get("direction") is not None else None),
            option_plan=str(payload.get("option_plan") or ""),
            invalidation_reference=str(payload.get("invalidation_reference") or ""),
            notes=str(payload.get("notes") or ""),
        )


@dataclass(frozen=True)
class DayAssessment:
    day_type: TraderDayType
    directional_bias: Optional[Direction]
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SetupAssessment:
    setup_type: TraderSetupType
    direction: Optional[Direction]
    score: float
    trigger_ready: bool
    invalidation_reference: str
    expected_move_pct: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class OptionAssessment:
    tradable: bool
    score: float
    reasons: tuple[str, ...]
    premium: Optional[float]
    liquidity_ratio: Optional[float]


@dataclass
class BreakoutCandidate:
    direction: Direction
    level: float
    breakout_minute: int
    retest_seen: bool = False
    retest_minute: Optional[int] = None


@dataclass
class PullbackCandidate:
    direction: Direction
    bias_minute: int
    pullback_seen: bool = False
    pullback_minute: Optional[int] = None


@dataclass
class FailedBreakoutCandidate:
    failed_direction: str
    level: float
    break_minute: int


@dataclass
class TraderSetupState:
    breakout: Optional[BreakoutCandidate] = None
    pullback: Optional[PullbackCandidate] = None
    failed_breakout: Optional[FailedBreakoutCandidate] = None


@dataclass(frozen=True)
class DayClassifierConfig:
    min_trend_r15m: float = 0.0007
    min_trend_r30m: float = 0.0014
    min_trend_vol_ratio: float = 1.10
    no_trade_iv_percentile: float = 97.0
    reversal_reentry_buffer_pct: float = 0.0002
    midday_no_trade_start_minute: int = 135
    midday_no_trade_end_minute: int = 255
    max_midday_abs_r15m: float = 0.0006
    max_midday_abs_price_vs_vwap: float = 0.0008
    max_midday_vol_ratio: float = 1.00


@dataclass(frozen=True)
class SetupScorerConfig:
    breakout_buffer_pct: float = 0.0006
    retest_tolerance_pct: float = 0.0012
    resume_buffer_pct: float = 0.0004
    invalidation_buffer_pct: float = 0.0012
    breakout_r5m_min: float = 0.0007
    resume_r5m_min: float = 0.0005
    min_r15m_confirm: float = 0.0005
    min_vol_ratio: float = 1.10
    max_breakout_age_minutes: int = 60
    pullback_distance_pct: float = 0.0020
    max_pullback_age_minutes: int = 90
    failed_break_reentry_buffer_pct: float = 0.0002
    failed_break_invalidation_pct: float = 0.0018
    failed_break_r5m_min: float = 0.0006
    max_failed_break_age_minutes: int = 45


@dataclass(frozen=True)
class OptionScorerConfig:
    min_option_vol_ratio: float = 1.00
    min_premium: float = 35.0
    max_premium: float = 350.0
    min_expected_move_pct: float = 0.0015
    max_iv_percentile: float = 96.0


@dataclass(frozen=True)
class TradeGovernorConfig:
    trend_day_max_entries: int = 2
    reversal_day_max_entries: int = 1
    balanced_day_max_entries: int = 0
    no_trade_day_max_entries: int = 0
    min_total_score_trend: float = 0.76
    min_total_score_reversal: float = 0.82
    min_total_score_balanced: float = 0.88
    min_option_score: float = 0.62
    min_setup_score: float = 0.78


@dataclass(frozen=True)
class TradeGovernorDecision:
    allowed: bool
    reason: str
    max_entries: int


class TraderDayClassifier:
    def __init__(self, config: Optional[DayClassifierConfig] = None) -> None:
        self._config = config or DayClassifierConfig()

    def assess(self, snap: SnapshotAccessor) -> DayAssessment:
        if not snap.is_valid_entry_phase:
            return DayAssessment(TraderDayType.NO_TRADE, None, 0.0, ("inactive_phase",))
        if snap.vix_spike_flag:
            return DayAssessment(TraderDayType.NO_TRADE, None, 0.0, ("vix_spike",))
        if snap.iv_percentile is not None and snap.iv_percentile >= self._config.no_trade_iv_percentile:
            return DayAssessment(TraderDayType.NO_TRADE, None, 0.05, ("extreme_iv",))

        close = snap.fut_close
        vwap = snap.vwap
        r15m = snap.fut_return_15m or 0.0
        r30m = snap.fut_return_30m or 0.0
        price_vs_vwap = snap.price_vs_vwap or 0.0
        vol_ratio = snap.vol_ratio or 0.0
        if (
            self._config.midday_no_trade_start_minute <= snap.minutes <= self._config.midday_no_trade_end_minute
            and abs(r15m) <= self._config.max_midday_abs_r15m
            and abs(price_vs_vwap) <= self._config.max_midday_abs_price_vs_vwap
            and vol_ratio <= self._config.max_midday_vol_ratio
        ):
            return DayAssessment(TraderDayType.NO_TRADE, None, 0.10, ("midday_low_energy",))
        if close is not None and vwap is not None and vol_ratio >= self._config.min_trend_vol_ratio:
            if close > vwap and r15m >= self._config.min_trend_r15m and r30m >= self._config.min_trend_r30m:
                return DayAssessment(TraderDayType.TREND, Direction.CE, 0.82, ("trend_up",))
            if close < vwap and r15m <= -self._config.min_trend_r15m and r30m <= -self._config.min_trend_r30m:
                return DayAssessment(TraderDayType.TREND, Direction.PE, 0.82, ("trend_down",))

        if snap.or_ready and close is not None and snap.orh is not None and snap.orl is not None:
            if snap.orh_broken and close < snap.orh * (1.0 - self._config.reversal_reentry_buffer_pct):
                return DayAssessment(TraderDayType.REVERSAL, Direction.PE, 0.72, ("failed_orh_acceptance",))
            if snap.orl_broken and close > snap.orl * (1.0 + self._config.reversal_reentry_buffer_pct):
                return DayAssessment(TraderDayType.REVERSAL, Direction.CE, 0.72, ("failed_orl_acceptance",))

        return DayAssessment(TraderDayType.BALANCED, None, 0.55, ("balanced_session",))


class TraderSetupScorer:
    def __init__(self, config: Optional[SetupScorerConfig] = None) -> None:
        self._config = config or SetupScorerConfig()

    def observe(self, snap: SnapshotAccessor, state: TraderSetupState) -> None:
        self._observe_breakout(snap, state)
        self._observe_pullback_bias(snap, state)
        self._observe_failed_breakout(snap, state)

    def best_setup(self, snap: SnapshotAccessor, state: TraderSetupState, day: DayAssessment) -> SetupAssessment:
        candidates = [
            self._score_orb_retest(snap, state, day),
            self._score_vwap_pullback(snap, state, day),
            self._score_failed_breakout(snap, state, day),
        ]
        ready = [item for item in candidates if item.trigger_ready]
        if not ready:
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        return max(ready, key=lambda item: item.score)

    def _observe_breakout(self, snap: SnapshotAccessor, state: TraderSetupState) -> None:
        if state.breakout is not None:
            return
        close = snap.fut_close
        r5m = snap.fut_return_5m
        vol_ratio = snap.vol_ratio or 0.0
        if close is None or r5m is None or not snap.or_ready or vol_ratio < self._config.min_vol_ratio:
            return
        if snap.orh is not None and snap.orh_broken and close > snap.orh * (1.0 + self._config.breakout_buffer_pct) and r5m >= self._config.breakout_r5m_min:
            state.breakout = BreakoutCandidate(direction=Direction.CE, level=snap.orh, breakout_minute=snap.minutes)
            return
        if snap.orl is not None and snap.orl_broken and close < snap.orl * (1.0 - self._config.breakout_buffer_pct) and r5m <= -self._config.breakout_r5m_min:
            state.breakout = BreakoutCandidate(direction=Direction.PE, level=snap.orl, breakout_minute=snap.minutes)

    def _observe_pullback_bias(self, snap: SnapshotAccessor, state: TraderSetupState) -> None:
        if state.pullback is not None:
            return
        close = snap.fut_close
        vwap = snap.vwap
        r15m = snap.fut_return_15m or 0.0
        r30m = snap.fut_return_30m or 0.0
        vol_ratio = snap.vol_ratio or 0.0
        if close is None or vwap is None or vol_ratio < self._config.min_vol_ratio:
            return
        if close > vwap and r15m >= self._config.min_r15m_confirm and r30m > 0.0:
            state.pullback = PullbackCandidate(direction=Direction.CE, bias_minute=snap.minutes)
            return
        if close < vwap and r15m <= -self._config.min_r15m_confirm and r30m < 0.0:
            state.pullback = PullbackCandidate(direction=Direction.PE, bias_minute=snap.minutes)

    def _observe_failed_breakout(self, snap: SnapshotAccessor, state: TraderSetupState) -> None:
        if state.failed_breakout is not None:
            return
        close = snap.fut_close
        r5m = snap.fut_return_5m
        vol_ratio = snap.vol_ratio or 0.0
        if close is None or r5m is None or not snap.or_ready or vol_ratio < self._config.min_vol_ratio:
            return
        if snap.orh is not None and snap.orh_broken and close > snap.orh * (1.0 + self._config.breakout_buffer_pct) and r5m >= self._config.failed_break_r5m_min:
            state.failed_breakout = FailedBreakoutCandidate(failed_direction="UP", level=snap.orh, break_minute=snap.minutes)
            return
        if snap.orl is not None and snap.orl_broken and close < snap.orl * (1.0 - self._config.breakout_buffer_pct) and r5m <= -self._config.failed_break_r5m_min:
            state.failed_breakout = FailedBreakoutCandidate(failed_direction="DOWN", level=snap.orl, break_minute=snap.minutes)

    def _score_orb_retest(self, snap: SnapshotAccessor, state: TraderSetupState, day: DayAssessment) -> SetupAssessment:
        breakout = state.breakout
        close = snap.fut_close
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        vol_ratio = snap.vol_ratio or 0.0
        if breakout is None or close is None or r5m is None or r15m is None:
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if snap.minutes - breakout.breakout_minute > self._config.max_breakout_age_minutes:
            state.breakout = None
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        level = breakout.level
        if breakout.direction == Direction.CE:
            if close < level * (1.0 - self._config.invalidation_buffer_pct):
                state.breakout = None
                return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
            if (
                not breakout.retest_seen
                and snap.minutes > breakout.breakout_minute
                and abs(close - level) / level <= self._config.retest_tolerance_pct
                and r5m > -self._config.resume_r5m_min
            ):
                breakout.retest_seen = True
                breakout.retest_minute = snap.minutes
            if breakout.retest_seen and close > level * (1.0 + self._config.resume_buffer_pct) and r5m >= self._config.resume_r5m_min and r15m >= self._config.min_r15m_confirm and vol_ratio >= self._config.min_vol_ratio and day.day_type in (TraderDayType.TREND, TraderDayType.BALANCED):
                expected_move_pct = max(abs(r15m) * 1.4, ((snap.or_width or 0.0) / max(close, 1.0)) * 0.6, 0.0020)
                return SetupAssessment(TraderSetupType.ORB_RETEST, Direction.CE, 0.84, True, "ORH", expected_move_pct, ("orb_retest_long",))
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if close > level * (1.0 + self._config.invalidation_buffer_pct):
            state.breakout = None
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if (
            not breakout.retest_seen
            and snap.minutes > breakout.breakout_minute
            and abs(close - level) / level <= self._config.retest_tolerance_pct
            and r5m < self._config.resume_r5m_min
        ):
            breakout.retest_seen = True
            breakout.retest_minute = snap.minutes
        if breakout.retest_seen and close < level * (1.0 - self._config.resume_buffer_pct) and r5m <= -self._config.resume_r5m_min and r15m <= -self._config.min_r15m_confirm and vol_ratio >= self._config.min_vol_ratio and day.day_type in (TraderDayType.TREND, TraderDayType.BALANCED):
            expected_move_pct = max(abs(r15m) * 1.4, ((snap.or_width or 0.0) / max(close, 1.0)) * 0.6, 0.0020)
            return SetupAssessment(TraderSetupType.ORB_RETEST, Direction.PE, 0.84, True, "ORL", expected_move_pct, ("orb_retest_short",))
        return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())

    def _score_vwap_pullback(self, snap: SnapshotAccessor, state: TraderSetupState, day: DayAssessment) -> SetupAssessment:
        pullback = state.pullback
        close = snap.fut_close
        vwap = snap.vwap
        price_vs_vwap = snap.price_vs_vwap
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        r30m = snap.fut_return_30m
        vol_ratio = snap.vol_ratio or 0.0
        if pullback is None or close is None or vwap is None or price_vs_vwap is None or r5m is None or r15m is None or r30m is None:
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if snap.minutes - pullback.bias_minute > self._config.max_pullback_age_minutes:
            state.pullback = None
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if day.day_type != TraderDayType.TREND:
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if pullback.direction == Direction.CE:
            if close < vwap * (1.0 - self._config.pullback_distance_pct):
                state.pullback = None
                return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
            if not pullback.pullback_seen and price_vs_vwap <= self._config.pullback_distance_pct and r5m <= 0.0:
                pullback.pullback_seen = True
                pullback.pullback_minute = snap.minutes
            if pullback.pullback_seen and close > vwap * (1.0 + self._config.resume_buffer_pct) and r5m >= self._config.resume_r5m_min and r15m >= self._config.min_r15m_confirm and vol_ratio >= self._config.min_vol_ratio:
                expected_move_pct = max(abs(r30m), abs(price_vs_vwap) * 2.0, 0.0022)
                return SetupAssessment(TraderSetupType.VWAP_PULLBACK, Direction.CE, 0.80, True, "VWAP", expected_move_pct, ("vwap_pullback_long",))
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if close > vwap * (1.0 + self._config.pullback_distance_pct):
            state.pullback = None
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if not pullback.pullback_seen and price_vs_vwap >= -self._config.pullback_distance_pct and r5m >= 0.0:
            pullback.pullback_seen = True
            pullback.pullback_minute = snap.minutes
        if pullback.pullback_seen and close < vwap * (1.0 - self._config.resume_buffer_pct) and r5m <= -self._config.resume_r5m_min and r15m <= -self._config.min_r15m_confirm and vol_ratio >= self._config.min_vol_ratio:
            expected_move_pct = max(abs(r30m), abs(price_vs_vwap) * 2.0, 0.0022)
            return SetupAssessment(TraderSetupType.VWAP_PULLBACK, Direction.PE, 0.80, True, "VWAP", expected_move_pct, ("vwap_pullback_short",))
        return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())

    def _score_failed_breakout(self, snap: SnapshotAccessor, state: TraderSetupState, day: DayAssessment) -> SetupAssessment:
        failed = state.failed_breakout
        close = snap.fut_close
        r5m = snap.fut_return_5m
        vol_ratio = snap.vol_ratio or 0.0
        if failed is None or close is None or r5m is None:
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if snap.minutes - failed.break_minute > self._config.max_failed_break_age_minutes:
            state.failed_breakout = None
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if day.day_type not in (TraderDayType.REVERSAL, TraderDayType.BALANCED):
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        level = failed.level
        if failed.failed_direction == "UP":
            if close > level * (1.0 + self._config.failed_break_invalidation_pct):
                state.failed_breakout = None
                return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
            if close < level * (1.0 - self._config.failed_break_reentry_buffer_pct) and r5m <= -self._config.failed_break_r5m_min and vol_ratio >= self._config.min_vol_ratio:
                expected_move_pct = max(abs(r5m) * 2.0, ((snap.or_width or 0.0) / max(close, 1.0)) * 0.5, 0.0018)
                return SetupAssessment(TraderSetupType.FAILED_BREAKOUT, Direction.PE, 0.78, True, "ORH", expected_move_pct, ("failed_breakout_short",))
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if close < level * (1.0 - self._config.failed_break_invalidation_pct):
            state.failed_breakout = None
            return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())
        if close > level * (1.0 + self._config.failed_break_reentry_buffer_pct) and r5m >= self._config.failed_break_r5m_min and vol_ratio >= self._config.min_vol_ratio:
            expected_move_pct = max(abs(r5m) * 2.0, ((snap.or_width or 0.0) / max(close, 1.0)) * 0.5, 0.0018)
            return SetupAssessment(TraderSetupType.FAILED_BREAKOUT, Direction.CE, 0.78, True, "ORL", expected_move_pct, ("failed_breakout_long",))
        return SetupAssessment(TraderSetupType.NONE, None, 0.0, False, "", 0.0, tuple())


class OptionTradabilityScorer:
    def __init__(self, config: Optional[OptionScorerConfig] = None) -> None:
        self._config = config or OptionScorerConfig()

    def assess(self, snap: SnapshotAccessor, direction: Direction, *, expected_move_pct: float) -> OptionAssessment:
        premium = snap.atm_ce_close if direction == Direction.CE else snap.atm_pe_close
        liquidity_ratio = snap.atm_ce_vol_ratio if direction == Direction.CE else snap.atm_pe_vol_ratio
        if premium is None or liquidity_ratio is None:
            return OptionAssessment(False, 0.0, ("missing_option_fields",), premium, liquidity_ratio)
        reasons: list[str] = []
        score = 0.55
        tradable = True
        if liquidity_ratio < self._config.min_option_vol_ratio:
            tradable = False
            reasons.append("thin_option_liquidity")
        else:
            score += min(0.20, (liquidity_ratio - self._config.min_option_vol_ratio) * 0.15)
        if premium < self._config.min_premium:
            tradable = False
            reasons.append("premium_too_low")
        elif premium > self._config.max_premium:
            tradable = False
            reasons.append("premium_too_expensive")
        else:
            score += 0.10
        if expected_move_pct < self._config.min_expected_move_pct:
            tradable = False
            reasons.append("expected_move_too_small")
        else:
            score += min(0.10, expected_move_pct / max(self._config.min_expected_move_pct, 1e-6) * 0.03)
        if snap.iv_percentile is not None and snap.iv_percentile > self._config.max_iv_percentile:
            tradable = False
            reasons.append("iv_too_rich")
        if not reasons:
            reasons.append("option_tradable")
        return OptionAssessment(tradable, min(score, 0.95), tuple(reasons), premium, liquidity_ratio)


class TradeGovernor:
    def __init__(self, config: Optional[TradeGovernorConfig] = None) -> None:
        self._config = config or TradeGovernorConfig()

    def evaluate(
        self,
        *,
        day: DayAssessment,
        setup: SetupAssessment,
        option: OptionAssessment,
        entries_taken: int,
    ) -> TradeGovernorDecision:
        max_entries = self._max_entries_for_day(day.day_type)
        if day.day_type == TraderDayType.NO_TRADE:
            return TradeGovernorDecision(False, "no_trade_day", max_entries)
        if day.day_type == TraderDayType.BALANCED:
            return TradeGovernorDecision(False, "balanced_day_skip", max_entries)
        if entries_taken >= max_entries:
            return TradeGovernorDecision(False, "session_entry_cap", max_entries)
        if option.score < self._config.min_option_score:
            return TradeGovernorDecision(False, "option_score_too_low", max_entries)
        if setup.score < self._config.min_setup_score:
            return TradeGovernorDecision(False, "setup_score_too_low", max_entries)
        total_score = (0.35 * day.score) + (0.45 * setup.score) + (0.20 * option.score)
        if day.day_type == TraderDayType.TREND:
            if setup.setup_type == TraderSetupType.FAILED_BREAKOUT:
                return TradeGovernorDecision(False, "trend_day_reversal_block", max_entries)
            if day.directional_bias is not None and setup.direction != day.directional_bias:
                return TradeGovernorDecision(False, "trend_bias_mismatch", max_entries)
            if total_score < self._config.min_total_score_trend:
                return TradeGovernorDecision(False, "trend_score_too_low", max_entries)
            return TradeGovernorDecision(True, "trend_setup_allowed", max_entries)
        if day.day_type == TraderDayType.REVERSAL:
            if setup.setup_type != TraderSetupType.FAILED_BREAKOUT:
                return TradeGovernorDecision(False, "reversal_requires_failed_breakout", max_entries)
            if total_score < self._config.min_total_score_reversal:
                return TradeGovernorDecision(False, "reversal_score_too_low", max_entries)
            return TradeGovernorDecision(True, "reversal_setup_allowed", max_entries)
        if total_score < self._config.min_total_score_balanced:
            return TradeGovernorDecision(False, "balanced_score_too_low", max_entries)
        return TradeGovernorDecision(False, "balanced_day_skip", max_entries)

    def _max_entries_for_day(self, day_type: TraderDayType) -> int:
        if day_type == TraderDayType.TREND:
            return self._config.trend_day_max_entries
        if day_type == TraderDayType.REVERSAL:
            return self._config.reversal_day_max_entries
        if day_type == TraderDayType.BALANCED:
            return self._config.balanced_day_max_entries
        return self._config.no_trade_day_max_entries


__all__ = [
    "BreakoutCandidate",
    "DayAssessment",
    "DayClassifierConfig",
    "FailedBreakoutCandidate",
    "OptionAssessment",
    "OptionScorerConfig",
    "TradeGovernor",
    "TradeGovernorConfig",
    "TradeGovernorDecision",
    "PullbackCandidate",
    "SetupAssessment",
    "SetupScorerConfig",
    "TraderAction",
    "TraderAnnotationRecord",
    "TraderDayClassifier",
    "TraderDayType",
    "TraderSetupScorer",
    "TraderSetupState",
    "TraderSetupType",
    "OptionTradabilityScorer",
]

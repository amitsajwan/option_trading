"""Deterministic strategy implementations."""

from __future__ import annotations

from typing import Optional

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


class ORBStrategy(BaseStrategy):
    """Opening range breakout with volume and PCR confirmation."""

    name = "ORB"

    def __init__(
        self,
        *,
        vol_ratio_min: float = 1.5,
        pcr_bull_min: float = 0.90,
        pcr_bear_max: float = 1.10,
        max_entry_minute: int = 135,
        confidence_base: float = 0.75,
    ) -> None:
        self._vol_ratio_min = vol_ratio_min
        self._pcr_bull_min = pcr_bull_min
        self._pcr_bear_max = pcr_bear_max
        self._max_entry_minute = max_entry_minute
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

        if not snap.is_valid_entry_phase or snap.minutes > self._max_entry_minute or not snap.or_ready:
            return None

        close = snap.fut_close
        vol_ratio = snap.vol_ratio
        pcr = snap.pcr
        orh = snap.orh
        orl = snap.orl
        if close is None or orh is None or orl is None:
            return None

        if snap.orh_broken and close > orh:
            confidence = self._confidence_base
            reasons = [f"close={close:.0f}>orh={orh:.0f}"]
            if vol_ratio is not None and vol_ratio >= self._vol_ratio_min:
                confidence += 0.10
                reasons.append(f"vol_ratio={vol_ratio:.2f}")
            else:
                confidence -= 0.15
            if pcr is not None and pcr >= self._pcr_bull_min:
                confidence += 0.05
                reasons.append(f"pcr={pcr:.2f}")
            else:
                confidence -= 0.10
            if confidence >= 0.50:
                return self._entry_vote(
                    snap,
                    direction=Direction.CE,
                    confidence=min(1.0, confidence),
                    reason="ORB_UP: " + ", ".join(reasons),
                    premium=snap.atm_ce_close,
                    raw_signals={"close": close, "orh": orh, "orl": orl, "vol_ratio": vol_ratio, "pcr": pcr},
                )

        if snap.orl_broken and close < orl:
            confidence = self._confidence_base
            reasons = [f"close={close:.0f}<orl={orl:.0f}"]
            if vol_ratio is not None and vol_ratio >= self._vol_ratio_min:
                confidence += 0.10
                reasons.append(f"vol_ratio={vol_ratio:.2f}")
            else:
                confidence -= 0.15
            if pcr is not None and pcr <= self._pcr_bear_max:
                confidence += 0.05
                reasons.append(f"pcr={pcr:.2f}")
            else:
                confidence -= 0.10
            if confidence >= 0.50:
                return self._entry_vote(
                    snap,
                    direction=Direction.PE,
                    confidence=min(1.0, confidence),
                    reason="ORB_DOWN: " + ", ".join(reasons),
                    premium=snap.atm_pe_close,
                    raw_signals={"close": close, "orh": orh, "orl": orl, "vol_ratio": vol_ratio, "pcr": pcr},
                )
        return None

    def _check_exit(self, snap: SnapshotAccessor, position: PositionContext) -> Optional[StrategyVote]:
        if not snap.or_ready or snap.fut_close is None or snap.orh is None or snap.orl is None:
            return None
        if position.direction == "CE" and snap.fut_close < snap.orh:
            return self._exit_vote(
                snap,
                f"ORB_REGIME_SHIFT: close={snap.fut_close:.0f}<orh={snap.orh:.0f}",
                ExitReason.REGIME_SHIFT,
                {"close": snap.fut_close, "orh": snap.orh, "orl": snap.orl},
            )
        if position.direction == "PE" and snap.fut_close > snap.orl:
            return self._exit_vote(
                snap,
                f"ORB_REGIME_SHIFT: close={snap.fut_close:.0f}>orl={snap.orl:.0f}",
                ExitReason.REGIME_SHIFT,
                {"close": snap.fut_close, "orh": snap.orh, "orl": snap.orl},
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
        oi_change_threshold: float = 0.02,
        confidence_base: float = 0.70,
        exit_r5m_threshold: float = 0.0,
        min_exit_hold_bars: int = 1,
    ) -> None:
        self._oi_change_threshold = oi_change_threshold
        self._confidence_base = confidence_base
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

        if not snap.is_valid_entry_phase or snap.minutes < 30:
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

        if oi_change_pct > self._oi_change_threshold and r15m > 0.001:
            reasons = [f"oi_chg={oi_change_pct:.2%}", f"r15m={r15m:.4f}"]
            if pcr is not None and pcr > 1.0:
                confidence += 0.05
                reasons.append(f"pcr={pcr:.2f}")
            if vol_ratio is not None and vol_ratio > 1.3:
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

        if oi_change_pct > self._oi_change_threshold and r15m < -0.001:
            reasons = [f"oi_chg={oi_change_pct:.2%}", f"r15m={r15m:.4f}"]
            if pcr is not None and pcr < 1.0:
                confidence += 0.05
                reasons.append(f"pcr={pcr:.2f}")
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

    def __init__(self, *, vol_ratio_min: float = 1.8, confidence_base: float = 0.72, max_entry_minute: int = 195) -> None:
        self._vol_ratio_min = vol_ratio_min
        self._confidence_base = confidence_base
        self._max_entry_minute = max_entry_minute

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


def build_default_strategy_set() -> list[BaseStrategy]:
    """Default registry order for the deterministic rule engine."""

    return [
        IVRegimeFilter(),
        ORBStrategy(),
        OIBuildupStrategy(),
        EMAcrossoverStrategy(),
        VWAPReclaimStrategy(),
        PrevDayLevelBreakout(),
    ]

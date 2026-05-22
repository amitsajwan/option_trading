"""Normalized intraday options-state helpers for trader-style strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from contracts_app.options_math import calculate_option_greeks, estimate_risk_free_rate

from ..contracts import Direction
from ..utils.env import safe_float as _safe_float
from .snapshot_accessor import SnapshotAccessor


@dataclass(frozen=True)
class OptionSideState:
    strike: int
    direction: Direction
    premium: Optional[float]
    iv: Optional[float]
    volume: Optional[float]
    oi: Optional[float]
    delta: Optional[float]
    delta_source: str
    distance_steps: int


@dataclass(frozen=True)
class StrikeState:
    strike: int
    ce: OptionSideState
    pe: OptionSideState


@dataclass(frozen=True)
class OptionsState:
    atm_strike: Optional[int]
    strike_step: Optional[int]
    strikes: tuple[StrikeState, ...]
    strike_count: int
    chain_quality: str
    iv_percentile: Optional[float]
    iv_skew: Optional[float]
    realized_vol_30m: Optional[float]
    ce_atm_premium: Optional[float]
    pe_atm_premium: Optional[float]
    ce_atm_liquidity_ratio: Optional[float]
    pe_atm_liquidity_ratio: Optional[float]

    def side_candidates(self, direction: Direction) -> list[OptionSideState]:
        if direction == Direction.CE:
            return [row.ce for row in self.strikes]
        if direction == Direction.PE:
            return [row.pe for row in self.strikes]
        return []


class OptionsStateBuilder:
    """Builds a normalized options view from the snapshot payload."""

    def build(self, snap: SnapshotAccessor) -> OptionsState:
        atm = snap.atm_strike
        step = snap.strike_step()
        raw_rows = snap.raw_payload.get("strikes")
        strikes: list[StrikeState] = []
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                strike = _safe_int(row.get("strike"))
                if strike is None:
                    continue
                distance_steps = _distance_steps(strike=strike, atm=atm, step=step)
                ce_delta, ce_source = self._resolve_delta(
                    snap=snap,
                    strike=strike,
                    option_type="call",
                    raw_value=row.get("ce_delta"),
                    raw_iv=row.get("ce_iv"),
                )
                pe_delta, pe_source = self._resolve_delta(
                    snap=snap,
                    strike=strike,
                    option_type="put",
                    raw_value=row.get("pe_delta"),
                    raw_iv=row.get("pe_iv"),
                )
                strikes.append(
                    StrikeState(
                        strike=strike,
                        ce=OptionSideState(
                            strike=strike,
                            direction=Direction.CE,
                            premium=_safe_float(row.get("ce_ltp")),
                            iv=_normalize_iv(row.get("ce_iv")),
                            volume=_safe_float(row.get("ce_volume")),
                            oi=_safe_float(row.get("ce_oi")),
                            delta=ce_delta,
                            delta_source=ce_source,
                            distance_steps=distance_steps,
                        ),
                        pe=OptionSideState(
                            strike=strike,
                            direction=Direction.PE,
                            premium=_safe_float(row.get("pe_ltp")),
                            iv=_normalize_iv(row.get("pe_iv")),
                            volume=_safe_float(row.get("pe_volume")),
                            oi=_safe_float(row.get("pe_oi")),
                            delta=pe_delta,
                            delta_source=pe_source,
                            distance_steps=distance_steps,
                        ),
                    )
                )
        strikes.sort(key=lambda item: item.strike)
        strike_count = len(strikes)
        if strike_count >= 7:
            chain_quality = "full"
        elif strike_count >= 3:
            chain_quality = "sparse"
        elif strike_count > 0:
            chain_quality = "thin"
        else:
            chain_quality = "missing"
        return OptionsState(
            atm_strike=atm,
            strike_step=step,
            strikes=tuple(strikes),
            strike_count=strike_count,
            chain_quality=chain_quality,
            iv_percentile=snap.iv_percentile,
            iv_skew=snap.iv_skew,
            realized_vol_30m=snap.realized_vol_30m,
            ce_atm_premium=snap.atm_ce_close,
            pe_atm_premium=snap.atm_pe_close,
            ce_atm_liquidity_ratio=snap.atm_ce_vol_ratio,
            pe_atm_liquidity_ratio=snap.atm_pe_vol_ratio,
        )

    def _resolve_delta(
        self,
        *,
        snap: SnapshotAccessor,
        strike: int,
        option_type: str,
        raw_value: object,
        raw_iv: object,
    ) -> tuple[Optional[float], str]:
        direct = _safe_float(raw_value)
        if direct is not None:
            return direct, "snapshot"
        iv = _normalize_iv(raw_iv)
        if iv is None:
            iv = snap.atm_ce_iv if option_type == "call" else snap.atm_pe_iv
            iv = _normalize_iv(iv)
        spot = snap.fut_close
        if spot is None or spot <= 0 or iv is None or iv <= 0:
            return None, "missing"
        days_to_expiry = snap.days_to_expiry
        time_to_expiry = max(float(days_to_expiry if days_to_expiry is not None else 1), 1.0) / 365.0
        greeks = calculate_option_greeks(
            float(spot),
            float(strike),
            time_to_expiry,
            estimate_risk_free_rate(),
            float(iv),
            option_type,
        )
        delta = _safe_float(greeks.get("delta"))
        if delta is None:
            return None, "missing"
        return delta, "estimated"


def _safe_int(value: object) -> Optional[int]:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _normalize_iv(value: object) -> Optional[float]:
    parsed = _safe_float(value)
    if parsed is None or parsed <= 0:
        return None
    if parsed > 1.0:
        return parsed / 100.0
    return parsed


def _distance_steps(*, strike: int, atm: Optional[int], step: Optional[int]) -> int:
    if atm is None or step is None or step <= 0:
        return 0
    return int(abs(strike - atm) // step)

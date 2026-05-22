"""Internal trader-style regime classification for V3 intraday playbooks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..contracts import Direction
from .options_state import OptionsState
from .snapshot_accessor import SnapshotAccessor


class TraderRegimeV3Label(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_CRUSH = "VOL_CRUSH"
    EXPIRY_MOMENTUM = "EXPIRY_MOMENTUM"
    EXPIRY_PINNING = "EXPIRY_PINNING"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class TraderRegimeV3:
    label: TraderRegimeV3Label
    bias: Optional[Direction]
    score: float
    reasons: tuple[str, ...]


class TraderRegimeClassifierV3:
    """Richer trader regime labels used only inside the V3 composite."""

    def assess(self, snap: SnapshotAccessor, options_state: OptionsState) -> TraderRegimeV3:
        if not snap.is_valid_entry_phase:
            return TraderRegimeV3(TraderRegimeV3Label.NO_TRADE, None, 0.0, ("inactive_phase",))
        if snap.vix_spike_flag:
            return TraderRegimeV3(TraderRegimeV3Label.NO_TRADE, None, 0.0, ("vix_spike",))

        minutes = snap.minutes
        r5m = snap.fut_return_5m or 0.0
        r15m = snap.fut_return_15m or 0.0
        r30m = snap.fut_return_30m or 0.0
        vol_ratio = snap.vol_ratio or 0.0
        price_vs_vwap = snap.price_vs_vwap or 0.0
        realized_vol = snap.realized_vol_30m or 0.0
        iv_pct = snap.iv_percentile or 0.0

        if snap.is_expiry_day:
            max_pain = snap.max_pain
            close = snap.fut_close
            if max_pain is not None and close is not None and close > 0:
                max_pain_gap = abs(close - max_pain) / close
                if max_pain_gap <= 0.0010 and vol_ratio <= 1.25 and abs(r15m) <= 0.0008:
                    return TraderRegimeV3(
                        TraderRegimeV3Label.EXPIRY_PINNING,
                        None,
                        0.82,
                        ("expiry_max_pain_cluster",),
                    )
            if vol_ratio >= 1.35 and abs(r15m) >= 0.0010 and abs(price_vs_vwap) >= 0.0008:
                bias = Direction.CE if r15m > 0 else Direction.PE
                return TraderRegimeV3(
                    TraderRegimeV3Label.EXPIRY_MOMENTUM,
                    bias,
                    0.84,
                    ("expiry_momentum",),
                )
            return TraderRegimeV3(TraderRegimeV3Label.NO_TRADE, None, 0.20, ("expiry_unclear",))

        if 135 <= minutes <= 255 and abs(r15m) <= 0.0006 and abs(price_vs_vwap) <= 0.0008 and vol_ratio <= 1.0:
            return TraderRegimeV3(TraderRegimeV3Label.NO_TRADE, None, 0.10, ("midday_low_energy",))

        if realized_vol >= 0.018 and vol_ratio >= 1.45 and abs(r15m) >= 0.0010:
            bias = Direction.CE if r15m > 0 else Direction.PE
            return TraderRegimeV3(TraderRegimeV3Label.VOL_EXPANSION, bias, 0.80, ("vol_expansion",))

        if iv_pct >= 75.0 and realized_vol <= 0.008 and vol_ratio <= 1.05 and abs(price_vs_vwap) <= 0.0010:
            return TraderRegimeV3(TraderRegimeV3Label.VOL_CRUSH, None, 0.72, ("vol_crush",))

        if r15m >= 0.0010 and r30m >= 0.0015 and price_vs_vwap >= 0.0008 and (snap.orh_broken or r5m >= 0.0004):
            return TraderRegimeV3(TraderRegimeV3Label.TREND_UP, Direction.CE, 0.84, ("trend_up",))
        if r15m <= -0.0010 and r30m <= -0.0015 and price_vs_vwap <= -0.0008 and (snap.orl_broken or r5m <= -0.0004):
            return TraderRegimeV3(TraderRegimeV3Label.TREND_DOWN, Direction.PE, 0.84, ("trend_down",))

        if options_state.chain_quality == "missing":
            return TraderRegimeV3(TraderRegimeV3Label.NO_TRADE, None, 0.15, ("missing_chain",))

        return TraderRegimeV3(TraderRegimeV3Label.RANGE, None, 0.58, ("range_session",))

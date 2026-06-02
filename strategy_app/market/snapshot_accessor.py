"""Typed accessor over the market snapshot payload."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from contracts_app import TimestampSourceMode, now_ist, parse_timestamp_to_ist


def _parse_hhmm(raw: str, default_h: int, default_m: int) -> int:
    """Parse 'HH:MM' → minutes since midnight. Returns default on any error."""
    try:
        parts = raw.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return default_h * 60 + default_m


class SnapshotAccessor:
    """Null-safe typed wrapper around the raw snapshot dict."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self._sc = payload.get("session_context") if isinstance(payload.get("session_context"), dict) else {}
        self._fb = payload.get("futures_bar") if isinstance(payload.get("futures_bar"), dict) else {}
        self._fd = payload.get("futures_derived") if isinstance(payload.get("futures_derived"), dict) else {}
        self._opening_range = payload.get("opening_range") if isinstance(payload.get("opening_range"), dict) else {}
        self._vix = payload.get("vix_context") if isinstance(payload.get("vix_context"), dict) else {}
        self._ca = payload.get("chain_aggregates") if isinstance(payload.get("chain_aggregates"), dict) else {}
        self._atm = payload.get("atm_options") if isinstance(payload.get("atm_options"), dict) else {}
        self._iv = payload.get("iv_derived") if isinstance(payload.get("iv_derived"), dict) else {}
        self._sl = payload.get("session_levels") if isinstance(payload.get("session_levels"), dict) else {}
        raw_strikes = payload.get("strikes") if isinstance(payload.get("strikes"), list) else []
        self._strikes = [row for row in raw_strikes if isinstance(row, dict)]
        self._strike_index: dict[int, dict[str, Any]] = {}
        for row in self._strikes:
            strike = self._i(row.get("strike"))
            if strike is None:
                continue
            self._strike_index[int(strike)] = row
        # velocity_enrichment: populated from 11:30 IST onwards by LiveVelocityAccumulator.
        # Key matches the canonical block name used by stage_views._project_view().
        self._vel = payload.get("velocity_enrichment") if isinstance(payload.get("velocity_enrichment"), dict) else {}

    @property
    def raw_payload(self) -> dict[str, Any]:
        return self._payload

    @staticmethod
    def _f(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed else None

    @classmethod
    def _first_present_float(cls, *values: Any) -> Optional[float]:
        for value in values:
            parsed = cls._f(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _i(value: Any) -> Optional[int]:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _b(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    @property
    def snapshot_id(self) -> str:
        return str(self._sc.get("snapshot_id") or self._payload.get("snapshot_id") or "")

    @property
    def timestamp(self) -> Optional[datetime]:
        raw = self._sc.get("timestamp") or self._payload.get("timestamp")
        if not raw:
            return None
        return parse_timestamp_to_ist(raw, naive_mode=TimestampSourceMode.MARKET_IST)

    @property
    def timestamp_or_now(self) -> datetime:
        return self.timestamp or now_ist()

    @property
    def trade_date(self) -> str:
        return str(self._sc.get("date") or self._payload.get("trade_date") or "")

    @property
    def minutes_since_open(self) -> Optional[int]:
        top_level = self._i(self._payload.get("minutes_since_open"))
        if top_level is not None:
            return top_level
        minute_index = self._i(self._payload.get("time_minute_index"))
        if minute_index is not None:
            return minute_index
        return self._i(self._sc.get("minutes_since_open"))

    @property
    def minutes(self) -> int:
        return self.minutes_since_open or 0

    @property
    def day_of_week(self) -> Optional[int]:
        return self._i(self._sc.get("day_of_week")) or self._i(self._payload.get("time_day_of_week"))

    @property
    def days_to_expiry(self) -> Optional[int]:
        return self._i(self._sc.get("days_to_expiry")) or self._i(self._payload.get("ctx_dte_days"))

    @property
    def is_expiry_day(self) -> bool:
        if "is_expiry_day" in self._sc:
            return self._b(self._sc.get("is_expiry_day"))
        return self._b(self._payload.get("ctx_is_expiry_day"))

    @property
    def ctx_is_high_vix_day(self) -> Optional[float]:
        return self._first_present_float(
            self._payload.get("ctx_is_high_vix_day"),
            self._sc.get("ctx_is_high_vix_day"),
            self._vix.get("ctx_is_high_vix_day"),
            self._payload.get("is_high_vix_day"),
        )

    @property
    def session_phase(self) -> str:
        phase = str(self._sc.get("session_phase") or self._payload.get("session_phase") or "").strip()
        if phase:
            return phase
        # Fallback: derive session phase from timestamp (IST) when the snapshot
        # producer omitted the session_context.session_phase field. Historical
        # snapshots captured before this field was added will hit this path.
        ts = self.timestamp
        if ts is None:
            return ""
        minute_of_day = int(ts.hour) * 60 + int(ts.minute)
        # Boundaries mirror snapshot_app.core.market_snapshot._session_phase.
        if 9 * 60 + 15 <= minute_of_day < 9 * 60 + 45:
            return "DISCOVERY"
        if 9 * 60 + 45 <= minute_of_day < 14 * 60 + 30:
            return "ACTIVE"
        if 14 * 60 + 30 <= minute_of_day <= 15 * 60 + 30:
            return "PRE_CLOSE"
        return "CLOSED"

    @property
    def is_valid_entry_phase(self) -> bool:
        # Default window matches model training distribution (09:45–14:30 IST).
        # Override via env vars — do not go below 09:35 (pre-open noise) or
        # above 15:05 (OOS for the model, thin liquidity near close).
        #   ENTRY_WINDOW_START_IST=09:45   (HH:MM format)
        #   ENTRY_WINDOW_END_IST=14:30     (HH:MM format, exclusive)
        start_raw = os.getenv("ENTRY_WINDOW_START_IST", "").strip()
        end_raw = os.getenv("ENTRY_WINDOW_END_IST", "").strip()
        if start_raw or end_raw:
            ts = self.timestamp
            if ts is None:
                return False
            minute = ts.hour * 60 + ts.minute
            start = _parse_hhmm(start_raw, 9, 45) if start_raw else 9 * 60 + 45
            end = _parse_hhmm(end_raw, 14, 30) if end_raw else 14 * 60 + 30
            return start <= minute < end
        # Default: honour the session_phase produced by snapshot_app — mirrors
        # training distribution (ACTIVE = 09:45–14:30 IST).
        return self.session_phase == "ACTIVE"

    @property
    def is_pre_close(self) -> bool:
        return self.session_phase == "PRE_CLOSE"

    @property
    def fut_open(self) -> Optional[float]:
        return self._f(self._fb.get("fut_open"))

    @property
    def fut_high(self) -> Optional[float]:
        return self._f(self._fb.get("fut_high"))

    @property
    def fut_low(self) -> Optional[float]:
        return self._f(self._fb.get("fut_low"))

    @property
    def fut_close(self) -> Optional[float]:
        return self._f(self._fb.get("fut_close"))

    @property
    def fut_volume(self) -> Optional[float]:
        return self._f(self._fb.get("fut_volume"))

    @property
    def fut_oi(self) -> Optional[float]:
        return self._f(self._fb.get("fut_oi"))

    @property
    def fut_return_5m(self) -> Optional[float]:
        return self._f(self._fd.get("fut_return_5m"))

    @property
    def fut_return_15m(self) -> Optional[float]:
        return self._f(self._fd.get("fut_return_15m"))

    @property
    def fut_return_30m(self) -> Optional[float]:
        return self._f(self._fd.get("fut_return_30m"))

    @property
    def realized_vol_30m(self) -> Optional[float]:
        return self._f(self._fd.get("realized_vol_30m"))

    @property
    def vol_ratio(self) -> Optional[float]:
        return self._f(self._fd.get("vol_ratio"))

    @property
    def fut_volume_ratio(self) -> Optional[float]:
        return self._f(self._fd.get("fut_volume_ratio"))

    @property
    def fut_oi_change_30m(self) -> Optional[float]:
        return self._f(self._fd.get("fut_oi_change_30m"))

    @property
    def ema_9(self) -> Optional[float]:
        return self._f(self._fd.get("ema_9"))

    @property
    def ema_21(self) -> Optional[float]:
        return self._f(self._fd.get("ema_21"))

    @property
    def ema_50(self) -> Optional[float]:
        return self._f(self._fd.get("ema_50"))

    @property
    def vwap(self) -> Optional[float]:
        return self._f(self._fd.get("vwap"))

    @property
    def price_vs_vwap(self) -> Optional[float]:
        return self._f(self._fd.get("price_vs_vwap"))

    @property
    def orh(self) -> Optional[float]:
        return self._f(self._opening_range.get("orh"))

    @property
    def orl(self) -> Optional[float]:
        return self._f(self._opening_range.get("orl"))

    @property
    def or_width(self) -> Optional[float]:
        return self._f(self._opening_range.get("or_width"))

    @property
    def price_vs_orh(self) -> Optional[float]:
        return self._f(self._opening_range.get("price_vs_orh"))

    @property
    def price_vs_orl(self) -> Optional[float]:
        return self._f(self._opening_range.get("price_vs_orl"))

    @property
    def orh_broken(self) -> bool:
        return self._b(self._opening_range.get("orh_broken"))

    @property
    def orl_broken(self) -> bool:
        return self._b(self._opening_range.get("orl_broken"))

    @property
    def or_ready(self) -> bool:
        return self.orh is not None and self.orl is not None

    @property
    def opening_range_width_pct(self) -> Optional[float]:
        """Opening range width as a fraction of the lower bound (ORL).

        Computed from existing orh/orl — no new snapshot data required.
        Returns None when opening range is not yet established.
        """
        orh = self.orh
        orl = self.orl
        if orh is None or orl is None or orl <= 0.0:
            return None
        return float((orh - orl) / orl)

    @property
    def candle_overlap(self) -> Optional[float]:
        """Bar-to-bar candle overlap ratio (0–1).

        Populated by RollingFeatureState.update() into futures_derived.
        Returns None during warmup (< 2 bars) or when not computed.
        """
        val = self._fd.get("candle_overlap")
        if val is None:
            val = self._payload.get("candle_overlap")
        return self._f(val)

    def _ctx_float(self, *keys: str) -> Optional[float]:
        for key in keys:
            if key in self._payload:
                parsed = self._f(self._payload.get(key))
                if parsed is not None:
                    return parsed
        return None

    @property
    def ctx_opening_range_ready(self) -> Optional[float]:
        return self._ctx_float("ctx_opening_range_ready", "opening_range_ready")

    @property
    def ctx_opening_range_breakout_down(self) -> Optional[float]:
        return self._ctx_float(
            "ctx_opening_range_breakout_down",
            "opening_range_breakout_down",
        )

    @property
    def ctx_opening_range_breakout_up(self) -> Optional[float]:
        return self._ctx_float(
            "ctx_opening_range_breakout_up",
            "opening_range_breakout_up",
        )

    @property
    def ctx_ret_5m(self) -> Optional[float]:
        return self._ctx_float("ret_5m", "ctx_ret_5m")

    @property
    def ctx_vwap_distance(self) -> Optional[float]:
        return self._ctx_float("vwap_distance", "ctx_vwap_distance")

    @property
    def vix_current(self) -> Optional[float]:
        return self._f(self._vix.get("vix_current"))

    @property
    def vix_prev_close(self) -> Optional[float]:
        return self._f(self._vix.get("vix_prev_close"))

    @property
    def vix_intraday_chg(self) -> Optional[float]:
        return self._f(self._vix.get("vix_intraday_chg"))

    @property
    def vix_regime(self) -> str:
        return str(self._vix.get("vix_regime") or "")

    @property
    def vix_spike_flag(self) -> bool:
        return self._b(self._vix.get("vix_spike_flag"))

    @property
    def atm_strike(self) -> Optional[int]:
        return self._i(self._ca.get("atm_strike")) or self._i(self._payload.get("opt_flow_atm_strike"))

    @property
    def strike_count(self) -> int:
        return len(self._strike_index)

    def available_strikes(self) -> list[int]:
        return sorted(int(key) for key in self._strike_index.keys())

    def strike_step(self) -> Optional[int]:
        strikes = self.available_strikes()
        if len(strikes) < 2:
            return None
        diffs = [b - a for a, b in zip(strikes, strikes[1:]) if (b - a) > 0]
        if not diffs:
            return None
        return int(min(diffs))

    @property
    def total_ce_oi(self) -> Optional[float]:
        return self._first_present_float(self._ca.get("total_ce_oi"), self._payload.get("opt_flow_ce_oi_total"))

    @property
    def total_pe_oi(self) -> Optional[float]:
        return self._first_present_float(self._ca.get("total_pe_oi"), self._payload.get("opt_flow_pe_oi_total"))

    @property
    def pcr(self) -> Optional[float]:
        return self._first_present_float(self._ca.get("pcr"), self._payload.get("opt_flow_pcr_oi"))

    @property
    def pcr_change_5m(self) -> Optional[float]:
        value = self._f(self._ca.get("pcr_change_5m"))
        return value if value is not None else self._f(self._payload.get("pcr_change_5m"))

    @property
    def pcr_change_15m(self) -> Optional[float]:
        value = self._f(self._ca.get("pcr_change_15m"))
        return value if value is not None else self._f(self._payload.get("pcr_change_15m"))

    @property
    def pcr_change_30m(self) -> Optional[float]:
        value = self._f(self._ca.get("pcr_change_30m"))
        return value if value is not None else self._f(self._payload.get("pcr_change_30m"))

    @property
    def max_pain(self) -> Optional[int]:
        return self._i(self._ca.get("max_pain")) or self._i(self._payload.get("max_pain"))

    @property
    def ce_oi_top_strike(self) -> Optional[int]:
        return self._i(self._ca.get("ce_oi_top_strike"))

    @property
    def pe_oi_top_strike(self) -> Optional[int]:
        return self._i(self._ca.get("pe_oi_top_strike"))

    @property
    def atm_ce_close(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_ce_close"))
        return value if value is not None else self._f(self._payload.get("atm_ce_close"))

    @property
    def atm_ce_open(self) -> Optional[float]:
        return self._f(self._atm.get("atm_ce_open"))

    @property
    def atm_ce_high(self) -> Optional[float]:
        return self._f(self._atm.get("atm_ce_high"))

    @property
    def atm_ce_low(self) -> Optional[float]:
        return self._f(self._atm.get("atm_ce_low"))

    @property
    def atm_pe_close(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_pe_close"))
        return value if value is not None else self._f(self._payload.get("atm_pe_close"))

    @property
    def atm_pe_open(self) -> Optional[float]:
        return self._f(self._atm.get("atm_pe_open"))

    @property
    def atm_pe_high(self) -> Optional[float]:
        return self._f(self._atm.get("atm_pe_high"))

    @property
    def atm_pe_low(self) -> Optional[float]:
        return self._f(self._atm.get("atm_pe_low"))

    @property
    def atm_ce_iv(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_ce_iv"))
        return value if value is not None else self._f(self._payload.get("atm_ce_iv"))

    @property
    def atm_pe_iv(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_pe_iv"))
        return value if value is not None else self._f(self._payload.get("atm_pe_iv"))

    @property
    def atm_ce_volume(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_ce_volume"))
        return value if value is not None else self._f(self._payload.get("atm_ce_volume"))

    @property
    def atm_pe_volume(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_pe_volume"))
        return value if value is not None else self._f(self._payload.get("atm_pe_volume"))

    @property
    def atm_ce_oi(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_ce_oi"))
        return value if value is not None else self._f(self._payload.get("atm_ce_oi"))

    @property
    def atm_pe_oi(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_pe_oi"))
        return value if value is not None else self._f(self._payload.get("atm_pe_oi"))

    @property
    def atm_oi_ratio(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_oi_ratio"))
        return value if value is not None else self._f(self._payload.get("atm_oi_ratio"))

    @property
    def atm_ce_oi_change_30m(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_ce_oi_change_30m"))
        return value if value is not None else self._f(self._payload.get("atm_ce_oi_change_30m"))

    @property
    def atm_pe_oi_change_30m(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_pe_oi_change_30m"))
        return value if value is not None else self._f(self._payload.get("atm_pe_oi_change_30m"))

    @property
    def atm_ce_vol_ratio(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_ce_vol_ratio"))
        return value if value is not None else self._f(self._payload.get("atm_ce_vol_ratio"))

    @property
    def atm_pe_vol_ratio(self) -> Optional[float]:
        value = self._f(self._atm.get("atm_pe_vol_ratio"))
        return value if value is not None else self._f(self._payload.get("atm_pe_vol_ratio"))

    @property
    def iv_skew(self) -> Optional[float]:
        return self._first_present_float(self._iv.get("iv_skew"), self._payload.get("iv_skew"))

    @property
    def near_atm_oi_ratio(self) -> Optional[float]:
        ladder = self._payload.get("ladder_aggregates") if isinstance(self._payload.get("ladder_aggregates"), dict) else {}
        value = self._f(ladder.get("near_atm_oi_ratio"))
        return value if value is not None else self._f(self._payload.get("near_atm_oi_ratio"))

    @property
    def iv_skew_dir(self) -> str:
        return str(self._iv.get("iv_skew_dir") or "")

    @property
    def iv_percentile(self) -> Optional[float]:
        return self._first_present_float(self._iv.get("iv_percentile"), self._payload.get("iv_percentile"))

    @property
    def iv_regime(self) -> str:
        return str(self._iv.get("iv_regime") or "")

    @property
    def iv_expiry_type(self) -> str:
        return str(self._iv.get("iv_expiry_type") or "")

    @property
    def prev_day_high(self) -> Optional[float]:
        return self._f(self._sl.get("prev_day_high"))

    @property
    def prev_day_low(self) -> Optional[float]:
        return self._f(self._sl.get("prev_day_low"))

    @property
    def prev_day_close(self) -> Optional[float]:
        return self._f(self._sl.get("prev_day_close"))

    @property
    def week_high(self) -> Optional[float]:
        return self._f(self._sl.get("week_high"))

    @property
    def week_low(self) -> Optional[float]:
        return self._f(self._sl.get("week_low"))

    @property
    def overnight_gap(self) -> Optional[float]:
        return self._f(self._sl.get("overnight_gap"))

    @property
    def prev_day_pcr(self) -> Optional[float]:
        return self._f(self._sl.get("prev_day_pcr"))

    @property
    def prev_day_max_pain(self) -> Optional[int]:
        return self._i(self._sl.get("prev_day_max_pain"))

    @property
    def atm_premium(self) -> Optional[float]:
        if self.atm_ce_close is not None and self.atm_pe_close is not None:
            return (self.atm_ce_close + self.atm_pe_close) / 2.0
        if self.atm_ce_close is not None:
            return self.atm_ce_close
        return self.atm_pe_close

    def option_ltp(self, direction: str, strike: Optional[int]) -> Optional[float]:
        strike_key = self._i(strike)
        if strike_key is None:
            return None
        row = self._strike_index.get(int(strike_key))
        side = str(direction or "").strip().upper()
        if not isinstance(row, dict):
            if self.atm_strike == strike_key:
                if side == "CE":
                    return self.atm_ce_close
                if side == "PE":
                    return self.atm_pe_close
            return None
        if side == "CE":
            return self._f(row.get("ce_ltp"))
        if side == "PE":
            return self._f(row.get("pe_ltp"))
        return None

    def option_oi(self, direction: str, strike: Optional[int]) -> Optional[float]:
        strike_key = self._i(strike)
        if strike_key is None:
            return None
        row = self._strike_index.get(int(strike_key))
        side = str(direction or "").strip().upper()
        if not isinstance(row, dict):
            if self.atm_strike == strike_key:
                if side == "CE":
                    return self.atm_ce_oi
                if side == "PE":
                    return self.atm_pe_oi
            return None
        if side == "CE":
            return self._f(row.get("ce_oi"))
        if side == "PE":
            return self._f(row.get("pe_oi"))
        return None

    def option_volume(self, direction: str, strike: Optional[int]) -> Optional[float]:
        strike_key = self._i(strike)
        if strike_key is None:
            return None
        row = self._strike_index.get(int(strike_key))
        side = str(direction or "").strip().upper()
        if not isinstance(row, dict):
            if self.atm_strike == strike_key:
                if side == "CE":
                    return self.atm_ce_volume
                if side == "PE":
                    return self.atm_pe_volume
            return None
        if side == "CE":
            return self._f(row.get("ce_volume"))
        if side == "PE":
            return self._f(row.get("pe_volume"))
        return None

    def option_ohlc(self, direction: str, strike: Optional[int]) -> Optional[dict[str, Optional[float]]]:
        strike_key = self._i(strike)
        if strike_key is None:
            return None
        row = self._strike_index.get(int(strike_key))
        if not isinstance(row, dict):
            return None

        side = str(direction or "").strip().upper()
        if side == "CE":
            return {
                "open": self._f(row.get("ce_open")),
                "high": self._f(row.get("ce_high")),
                "low": self._f(row.get("ce_low")),
                "close": self._f(row.get("ce_ltp")),
            }
        if side == "PE":
            return {
                "open": self._f(row.get("pe_open")),
                "high": self._f(row.get("pe_high")),
                "low": self._f(row.get("pe_low")),
                "close": self._f(row.get("pe_ltp")),
            }
        return None

    # ------------------------------------------------------------------
    # velocity_features — populated from 11:30 IST by LiveVelocityAccumulator
    # ------------------------------------------------------------------

    @property
    def has_velocity(self) -> bool:
        """True if velocity features were computed for this tick (post-11:30 IST)."""
        return bool(self._vel)

    def vel(self, name: str) -> Optional[float]:
        """Return a single velocity feature by name, or None if missing/NaN."""
        return self._f(self._vel.get(name))

    @property
    def velocity_features(self) -> dict[str, Any]:
        """Full velocity feature dict (may be empty before 11:30 IST)."""
        return self._vel

"""Tests for snapshot_app.core.live_velocity_state.LiveVelocityAccumulator."""

from __future__ import annotations

import math
from typing import Any, Dict

import pytest

from snapshot_app.core.live_velocity_state import LiveVelocityAccumulator


# ── helpers ────────────────────────────────────────────────────────────────────

def _snap(
    trade_date: str,
    hour: int,
    minute: int,
    *,
    ce_oi: float = 500_000.0,
    pe_oi: float = 400_000.0,
    fut_close: float = 22_000.0,
    pcr: float = 0.8,
) -> Dict[str, Any]:
    """Build a minimal live snapshot dict for the accumulator."""
    ts = f"{trade_date}T{hour:02d}:{minute:02d}:00+05:30"
    return {
        "trade_date": trade_date,
        "timestamp": ts,
        "snapshot_id": f"{trade_date.replace('-', '')}_{hour:02d}{minute:02d}",
        "futures_bar": {
            "fut_open": fut_close - 30.0,
            "fut_high": fut_close + 50.0,
            "fut_low": fut_close - 40.0,
            "fut_close": fut_close,
        },
        "futures_derived": {"vwap": fut_close - 10.0},
        "chain_aggregates": {
            "total_ce_oi": ce_oi,
            "total_pe_oi": pe_oi,
            "total_ce_volume": 120_000.0,
            "total_pe_volume": 90_000.0,
            "pcr": pcr,
            "pcr_change_15m": 0.02,
        },
        "atm_options": {
            "atm_oi_ratio": ce_oi / (ce_oi + pe_oi),
            "atm_ce_iv": 0.18,
            "atm_pe_iv": 0.16,
        },
        "iv_derived": {"iv_skew": 0.02},
    }


def _morning_snaps(trade_date: str, n: int = 7) -> list[Dict[str, Any]]:
    """Generate n snapshots starting at 10:00 with 15-minute spacing."""
    snaps = []
    for i in range(n):
        total_min = 10 * 60 + i * 15
        h, m = divmod(total_min, 60)
        snaps.append(_snap(
            trade_date, h, m,
            ce_oi=500_000.0 + i * 10_000.0,
            pe_oi=400_000.0 + i * 5_000.0,
            fut_close=22_000.0 + i * 20.0,
            pcr=0.8 + i * 0.01,
        ))
    return snaps


# ── tests ──────────────────────────────────────────────────────────────────────

class TestAccumulatorBasicFlow:
    def test_velocity_available_from_third_bar(self) -> None:
        """Per-bar engine (no 11:30 restriction): velocity is injected once >=3
        bars have accumulated from session open — NOT gated to midday."""
        acc = LiveVelocityAccumulator()
        results = [acc.process(snap) for snap in _morning_snaps("2026-04-18", n=6)]
        # bars 1,2 (<3 rows) → no velocity; bar 3 onward → velocity present
        assert "velocity_enrichment" not in results[0]
        assert "velocity_enrichment" not in results[1]
        assert "velocity_enrichment" in results[2], "velocity should appear at the 3rd bar"
        assert "velocity_enrichment" in results[-1]

    def test_velocity_injected_at_1130(self) -> None:
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):  # 10:00 → 11:30
            result = acc.process(snap)
        # Last snap was 11:30 — velocity should now be present
        assert "velocity_enrichment" in result
        vel = result["velocity_enrichment"]
        assert isinstance(vel, dict)
        assert len(vel) > 0

    def test_velocity_injected_on_post_midday_snaps(self) -> None:
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):
            acc.process(snap)
        # Post-11:30 tick
        post = _snap("2026-04-18", 12, 0)
        result = acc.process(post)
        assert "velocity_enrichment" in result

    def test_velocity_values_are_finite_for_valid_input(self) -> None:
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):
            result = acc.process(snap)
        vel = result.get("velocity_enrichment", {})
        valid_count = sum(1 for v in vel.values() if isinstance(v, float) and math.isfinite(v))
        assert valid_count >= 10, f"expected at least 10 finite velocity values, got {valid_count}"

    def test_original_snapshot_fields_preserved(self) -> None:
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):
            result = acc.process(snap)
        assert result["trade_date"] == "2026-04-18"
        assert "futures_bar" in result
        assert "chain_aggregates" in result


class TestDayBoundaryReset:
    def test_velocity_recomputed_on_new_day(self) -> None:
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):
            acc.process(snap)
        # new day — state reset, single first bar (<3 rows) → no velocity yet
        early = _snap("2026-04-22", 9, 15)
        result = acc.process(early)
        assert "velocity_enrichment" not in result

    def test_velocity_available_on_second_day_after_1130(self) -> None:
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):
            acc.process(snap)
        for snap in _morning_snaps("2026-04-22", n=7):
            result = acc.process(snap)
        assert "velocity_enrichment" in result

    def test_second_day_velocity_is_fresh_not_leaked(self) -> None:
        """Day boundary resets accumulated bars: the 2nd day's velocity is
        computed only from 2nd-day data, never carrying 1st-day values."""
        acc = LiveVelocityAccumulator()
        for snap in _morning_snaps("2026-04-18", n=7):
            acc.process(snap)
        day1_vel = dict(acc._velocity or {})
        # Next day — first 2 bars insufficient, 3rd bar yields fresh velocity
        results = [acc.process(snap) for snap in _morning_snaps("2026-04-22", n=3)]
        assert "velocity_enrichment" not in results[0]
        assert "velocity_enrichment" in results[2]
        day2_vel = results[2]["velocity_enrichment"]
        # price_delta_open is anchored to the day's own open → must differ across days
        assert day2_vel.get("vel_price_delta_open") != day1_vel.get("vel_price_delta_open")


class TestInsufficientMorningRows:
    def test_no_velocity_when_fewer_than_3_rows(self) -> None:
        """<3 accumulated bars → no velocity (need a minimal window to compute)."""
        acc = LiveVelocityAccumulator()
        results = [acc.process(snap) for snap in _morning_snaps("2026-04-18", n=2)]
        for result in results:
            assert "velocity_enrichment" not in result


class TestPerBarUpdates:
    def test_velocity_updates_each_bar(self) -> None:
        """Per-bar engine recomputes on every new bar (no compute-once gate).
        As fresh bars arrive, velocity reflects the latest data."""
        acc = LiveVelocityAccumulator()
        snaps = _morning_snaps("2026-04-18", n=7)
        for snap in snaps[:3]:
            acc.process(snap)
        vel_at_3 = dict(acc._velocity or {})
        for snap in snaps[3:]:
            acc.process(snap)
        vel_at_7 = dict(acc._velocity or {})
        # more bars + rising price/OI in the fixture → delta_open grows
        assert vel_at_7.get("vel_price_delta_open") != vel_at_3.get("vel_price_delta_open")

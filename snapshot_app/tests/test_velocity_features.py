"""Unit tests for snapshot_app.core.velocity_features."""

from __future__ import annotations

import math
from typing import Any, Dict, List

import pandas as pd
import pytest

from snapshot_app.core.velocity_features import (
    VELOCITY_COLUMNS,
    compute_velocity_features,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_timestamp(date: str, hour: int, minute: int) -> pd.Timestamp:
    return pd.Timestamp(f"{date} {hour:02d}:{minute:02d}:00")


def _make_morning_df(
    trade_date: str = "2023-06-15",
    n_rows: int = 7,
    ce_oi_start: float = 500_000.0,
    ce_oi_step: float = 10_000.0,
    pe_oi_start: float = 400_000.0,
    pe_oi_step: float = 5_000.0,
    price_start: float = 44_000.0,
    price_step: float = 50.0,
    pcr_start: float = 1.2,
    pcr_step: float = -0.05,
    atm_oi_ratio_start: float = 1.1,
    atm_oi_ratio_step: float = 0.03,
    atm_ce_iv: float = 0.20,
    atm_pe_iv: float = 0.18,
) -> pd.DataFrame:
    """
    Build a synthetic morning_df with n_rows rows starting at 10:00,
    15 minutes apart.
    """
    rows: List[Dict[str, Any]] = []
    for i in range(n_rows):
        total_min = 10 * 60 + i * 15
        hour = total_min // 60
        minute = total_min % 60
        ts = _make_timestamp(trade_date, hour, minute)
        rows.append({
            "trade_date": trade_date,
            "timestamp": ts,
            "opt_flow_ce_oi_total": ce_oi_start + i * ce_oi_step,
            "opt_flow_pe_oi_total": pe_oi_start + i * pe_oi_step,
            "opt_flow_pcr_oi": pcr_start + i * pcr_step,
            "atm_oi_ratio": atm_oi_ratio_start + i * atm_oi_ratio_step,
            "px_fut_close": price_start + i * price_step,
            "px_fut_open": price_start + i * price_step - 10.0,
            "px_fut_high": price_start + i * price_step + 30.0,
            "px_fut_low": price_start + i * price_step - 20.0,
            "opt_flow_ce_volume_total": 50_000.0 + i * 2_000.0,
            "opt_flow_pe_volume_total": 40_000.0 + i * 1_500.0,
            "vwap_fut": price_start + price_step * n_rows / 2.0,
            "pcr_change_15m": pcr_step,
            "ctx_opening_range_breakout_up": 0,
            "ctx_opening_range_breakout_down": 0,
            # IV columns
            "atm_ce_iv": atm_ce_iv - i * 0.002,
            "atm_pe_iv": atm_pe_iv - i * 0.001,
            "iv_skew": (atm_ce_iv - i * 0.002) - (atm_pe_iv - i * 0.001),
        })
    return pd.DataFrame(rows)


def _make_midday(morning_df: pd.DataFrame) -> pd.Series:
    """Return the last row of morning_df as the midday snapshot."""
    return morning_df.iloc[-1].copy()


# ── test: normal day ───────────────────────────────────────────────────────────

def test_velocity_features_normal_day() -> None:
    """All columns present and non-NaN for a standard 7-row morning."""
    morning = _make_morning_df(n_rows=7)
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday, prev_day_close=43_900.0)

    # every output column must be present
    for col in VELOCITY_COLUMNS:
        assert col in result, f"missing column: {col}"

    # OI delta from open should be positive (step is +10k per row)
    assert result["vel_ce_oi_delta_open"] > 0
    assert result["vel_pe_oi_delta_open"] > 0

    # PCR was falling (negative step) → delta_open < 0
    assert result["vel_pcr_delta_open"] < 0

    # price was rising → delta_open > 0
    assert result["vel_price_delta_open"] > 0

    # ctx_am_price_position must be in [0, 1]
    pos = result["ctx_am_price_position"]
    assert math.isfinite(pos) and 0.0 <= pos <= 1.0, f"price_position out of range: {pos}"

    # build rates finite
    assert math.isfinite(result["vel_ce_oi_build_rate"])
    assert math.isfinite(result["vel_pe_oi_build_rate"])


# ── test: fewer than 3 morning snapshots → all NaN ────────────────────────────

def test_velocity_features_missing_snapshots() -> None:
    """Fewer than 3 morning rows → every output must be NaN."""
    morning = _make_morning_df(n_rows=2)
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday)

    for col in VELOCITY_COLUMNS:
        val = result[col]
        assert math.isnan(val), f"{col} should be NaN with 2 morning rows, got {val}"


# ── test: flat market (price barely moves) ────────────────────────────────────

def test_velocity_features_flat_market() -> None:
    """Flat price day → ctx_am_trend = 0, range_size is small."""
    morning = _make_morning_df(n_rows=7, price_step=0.5)
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday)

    assert result["ctx_am_trend"] == 0.0, f"expected trend=0, got {result['ctx_am_trend']}"
    # range_size should be small (<10 usually), so price_position may be NaN due to clipping threshold
    assert math.isfinite(result["ctx_am_range_size"])
    assert result["ctx_am_range_size"] >= 0


# ── test: gap up that gets filled ─────────────────────────────────────────────

def test_velocity_features_gap_filled() -> None:
    """
    Gap up at open: open > prev_close.
    Price falls back below prev_close by 11:30 → ctx_am_gap_filled = 1.
    """
    prev_close = 44_000.0
    # price starts at 44_200 (gap up 200), then falls each step by 50
    morning = _make_morning_df(n_rows=7, price_start=44_200.0, price_step=-50.0)
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday, prev_day_close=prev_close)

    assert math.isfinite(result["ctx_am_gap_from_yday"]), "gap_from_yday should be finite"
    assert result["ctx_am_gap_from_yday"] > 0, "gap should be positive (gap up)"
    # price at 11:30 = 44_200 + 6*(-50) = 43_900 < prev_close 44_000
    assert result["ctx_am_gap_filled"] == 1.0, f"gap should be filled, got {result['ctx_am_gap_filled']}"


# ── test: CE OI building → vel_ce_oi_delta_open > 0 ──────────────────────────

def test_velocity_features_oi_building_ce() -> None:
    """CE OI increases every bar → vel_ce_oi_delta_open positive, ctx_am_oi_direction = 1."""
    morning = _make_morning_df(n_rows=7, ce_oi_step=20_000.0, pe_oi_step=1_000.0)
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday)

    assert result["vel_ce_oi_delta_open"] > 0
    assert result["ctx_am_oi_direction"] == 1.0, (
        f"expected oi_direction=1 when CE is building strongly, got {result['ctx_am_oi_direction']}"
    )


# ── test: no prev_day_close → gap features NaN ────────────────────────────────

def test_velocity_features_no_prev_day_close() -> None:
    """prev_day_close=None → ctx_am_gap_from_yday and ctx_am_gap_filled must be NaN."""
    morning = _make_morning_df(n_rows=7)
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday, prev_day_close=None)

    assert math.isnan(result["ctx_am_gap_from_yday"]), "gap_from_yday should be NaN without prev_close"
    assert math.isnan(result["ctx_am_gap_filled"]), "gap_filled should be NaN without prev_close"

    # all other non-gap columns should still compute normally
    assert math.isfinite(result["vel_ce_oi_delta_open"])
    assert math.isfinite(result["vel_price_delta_open"])


# ── test: IV columns absent from morning_df → IV velocity NaN ─────────────────

def test_velocity_features_no_iv_columns() -> None:
    """Morning df without IV columns → vel_atm_*_iv_* should be NaN, rest computes."""
    morning = _make_morning_df(n_rows=7)
    # drop IV columns
    morning = morning.drop(columns=["atm_ce_iv", "atm_pe_iv", "iv_skew"], errors="ignore")
    midday = _make_midday(morning)
    result = compute_velocity_features(morning, midday_snapshot=midday)

    assert math.isnan(result["vel_atm_ce_iv_delta_open"]), "IV delta should be NaN when IV absent"
    assert math.isnan(result["vel_atm_pe_iv_delta_open"]), "IV delta should be NaN when IV absent"
    assert math.isnan(result["vel_iv_compression_rate"]), "IV rate should be NaN when IV absent"

    # price and OI features must still compute
    assert math.isfinite(result["vel_price_delta_open"])
    assert math.isfinite(result["vel_ce_oi_delta_open"])

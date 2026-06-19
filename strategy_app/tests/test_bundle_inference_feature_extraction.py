"""Integration test: build_feature_row must return all compression model features
as finite (non-NaN) values given a realistic nested snapshot.

Catches the class of bug where a merge ordering issue (last-write-wins) silently
overwrites a good value from futures_derived with NaN from velocity_enrichment.
"""
from __future__ import annotations

import math

from snapshot_app.core.compression_features import COMPRESSION_FEATURE_COLUMNS
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.ml.bundle_inference import build_feature_row

# Minimal feature list covering all compression columns + a few staples.
# In production this comes from the bundle; here we test the extraction path.
_FEATURES = list(COMPRESSION_FEATURE_COLUMNS) + [
    "fut_return_5m",
    "realized_vol_30m",
    "atr_14_1m",
    "pcr",
    "vix_current",
]


def _make_snap(
    futures_derived_override: dict | None = None,
    velocity_enrichment_override: dict | None = None,
) -> SnapshotAccessor:
    fd: dict = {
        "fut_return_1m": 0.001,
        "fut_return_3m": 0.002,
        "fut_return_5m": 0.003,
        "fut_return_15m": 0.005,
        "fut_return_30m": 0.008,
        "realized_vol_30m": 0.12,
        "vol_ratio": 1.1,
        "fut_volume_ratio": 0.9,
        "fut_oi_change_30m": 500.0,
        "ema_9": 57800.0,
        "ema_21": 57750.0,
        "ema_50": 57700.0,
        "ema_9_slope": 2.0,
        "ema_21_slope": 1.5,
        "ema_50_slope": 1.0,
        "vwap": 57780.0,
        "price_vs_vwap": 0.001,
        "vwap_anchored_open": 57760.0,
        "price_vs_vwap_anchored": 0.0005,
        "atr_ratio": 0.0012,
        "atr_daily_percentile": 0.6,
        "dist_from_day_high": -0.002,
        "dist_from_day_low": 0.015,
        # All compression features with realistic non-NaN values.
        "bb_width_20": 0.0007,
        "bb_width_chg_5": 0.00005,
        "range_10": 0.0008,
        "range_30": 0.0012,
        "range_ratio_10_30": 0.67,
        "candle_overlap_10": 0.55,
        "ema_spread_9_21": 50.0,
        "ema_spread_21_50": 50.0,
        "ema_order": 1.0,
        "dist_from_ema21": 0.0009,
        "position_in_day_range": 0.65,
        "compression_score": 3.0,
        "adx_14": 22.5,
        "vol_spike_ratio": 0.89,
    }
    if futures_derived_override:
        fd.update(futures_derived_override)

    # velocity_enrichment intentionally has NaN for compression keys to
    # simulate the live velocity state that lacks rolling OHLCV history.
    vel: dict = {col: float("nan") for col in COMPRESSION_FEATURE_COLUMNS}
    vel.update({
        "vel_pcr_trend_direction": 1.0,
        "vel_ce_vol_delta_30m": 0.05,
        "vel_pe_vol_delta_30m": -0.03,
        "ctx_am_trend": 1.0,
        "ctx_gap_pct": 0.002,
    })
    if velocity_enrichment_override:
        vel.update(velocity_enrichment_override)

    return SnapshotAccessor({
        "snapshot_id": "test_20240801_1130",
        "instrument": "BANKNIFTY26JUNFUT",
        "trade_date": "2024-08-01",
        "timestamp": "2024-08-01T11:30:00+05:30",
        "session_context": {
            "session_phase": "ACTIVE",
            "minutes_since_open": 90,
            "days_to_expiry": 3,
            "is_expiry_day": False,
            "day_of_week": 3,
        },
        "futures_bar": {
            "fut_open": 57780.0,
            "fut_high": 57820.0,
            "fut_low": 57760.0,
            "fut_close": 57800.0,
            "fut_volume": 420,
            "fut_oi": 2168000,
        },
        "futures_derived": fd,
        "velocity_enrichment": vel,
        "mtf_derived": {"atr_14_1m": 35.0, "rsi_14_1m": 52.0, "rsi_14_5m": 54.0, "bb_width_5m": 0.0009, "mtf_aligned": 1.0},
        "chain_aggregates": {"pcr": 0.92, "pcr_change_30m": 0.05, "ce_pe_oi_diff": 0.1, "ce_pe_volume_diff": -0.05, "atm_straddle_pct": 0.018, "distance_to_max_pain_pct": 0.003},
        "vix_context": {"vix_current": 14.2, "vix_intraday_chg": -0.3, "vix_regime": "LOW", "vix_spike_flag": 0},
        "session_levels": {"prev_day_high": 58100.0, "prev_day_low": 57400.0, "prev_day_close": 57750.0},
        "opening_range": {"opening_range_ready": 1, "or_width_pct": 0.0015, "price_vs_orh": 0.002, "price_vs_orl": 0.008, "orh_broken": 0, "orl_broken": 0, "bars_since_or_break_up": 0, "bars_since_or_break_down": 0},
        "ladder_aggregates": {"near_atm_pcr": 0.95, "near_atm_oi_concentration": 0.42, "near_atm_volume_concentration": 0.38},
    })


def test_compression_features_non_nan_in_feature_row() -> None:
    """All compression model features must be finite when futures_derived has good values."""
    snap = _make_snap()
    row = build_feature_row(snap, _FEATURES)
    assert row is not None, "build_feature_row returned None"

    nan_compression = [
        col for col in COMPRESSION_FEATURE_COLUMNS
        if col in row and not math.isfinite(row[col])
    ]
    assert not nan_compression, (
        f"Compression features were NaN after extraction: {nan_compression}. "
        "Likely a merge-order bug where velocity_enrichment overwrote futures_derived."
    )


def test_velocity_nan_does_not_overwrite_futures_derived_value() -> None:
    """When futures_derived has a value and velocity_enrichment has NaN for the same
    key, the futures_derived value must survive into the feature row."""
    snap = _make_snap(
        futures_derived_override={"vol_spike_ratio": 1.23, "adx_14": 19.5},
        velocity_enrichment_override={"vol_spike_ratio": float("nan"), "adx_14": float("nan")},
    )
    row = build_feature_row(snap, _FEATURES)
    assert row is not None

    assert math.isfinite(row.get("vol_spike_ratio", float("nan"))), \
        "vol_spike_ratio became NaN — velocity_enrichment overwrote futures_derived"
    assert math.isfinite(row.get("adx_14", float("nan"))), \
        "adx_14 became NaN — velocity_enrichment overwrote futures_derived"

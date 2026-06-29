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


# ── Direction model feature parity tests ─────────────────────────────────────
# After retraining with snapshot field names, models use unprefixed feature names.
# This test verifies _compute_essential_features + stage views resolve all features.

_DIRECTION_FEATURES = [
    "iv_skew", "atm_iv", "iv_pct_rank_session", "vel_iv_skew_delta_open",
    "vel_iv_compression_rate", "vel_atm_ce_iv_delta_open", "vel_atm_pe_iv_delta_open",
    "pcr", "pcr_change_5m", "pcr_change_15m", "pcr_change_30m",
    "vel_pcr_delta_open", "vel_pcr_acceleration", "vel_pcr_trend_direction",
    "atm_ce_oi", "atm_pe_oi",
    "vix_current", "vix_open_day", "vix_intraday_chg", "is_high_vix_day",
    "ctx_gap_pct", "ctx_gap_up", "ctx_gap_down", "ctx_am_gap_filled",
    "minute_of_day", "day_of_week",
]


def _make_direction_snap() -> SnapshotAccessor:
    """Build a snapshot with all blocks populated for direction feature extraction."""
    return SnapshotAccessor({
        "snapshot_id": "test_20240801_1130",
        "instrument": "BANKNIFTY26AUGFUT",
        "trade_date": "2024-08-01",
        "timestamp": "2024-08-01T11:30:00+05:30",
        "session_context": {
            "snapshot_id": "test_20240801_1130",
            "timestamp": "2024-08-01T11:30:00+05:30",
            "date": "2024-08-01",
            "time": "11:30",
            "minutes_since_open": 135,
            "minutes_to_close": 240,
            "day_of_week": 3,
            "days_to_expiry": 3,
            "is_expiry_day": False,
            "session_phase": "ACTIVE",
            "is_first_hour": False,
            "is_last_hour": False,
        },
        "futures_bar": {
            "fut_open": 57780.0, "fut_high": 57820.0, "fut_low": 57760.0,
            "fut_close": 57800.0, "fut_volume": 420, "fut_oi": 2168000,
        },
        "futures_derived": {
            "fut_return_1m": 0.001, "fut_return_5m": 0.003,
            "ema_9": 57800.0, "ema_21": 57750.0, "ema_50": 57700.0,
            "vwap": 57780.0, "price_vs_vwap": 0.001,
        },
        "vix_context": {
            "vix_current": 14.2, "vix_prev_close": 13.8,
            "vix_intraday_chg": -0.3, "vix_regime": "LOW", "vix_spike_flag": 0,
        },
        "chain_aggregates": {
            "pcr": 0.92, "pcr_change_5m": 0.002, "pcr_change_15m": -0.046,
            "pcr_change_30m": -0.127, "ce_pe_oi_diff": 0.1,
        },
        "atm_options": {
            "atm_ce_oi": 125000.0, "atm_pe_oi": 118000.0,
            "atm_ce_iv": 0.124, "atm_pe_iv": 0.128,
            "atm_ce_close": 220.0, "atm_pe_close": 210.0,
        },
        "iv_derived": {
            "iv_skew": -0.004, "iv_skew_dir": -1, "iv_percentile": 0.45,
            "iv_regime": "NORMAL", "iv_expiry_type": "weekly",
        },
        "velocity_enrichment": {
            "vel_iv_skew_delta_open": -0.005,
            "vel_iv_compression_rate": -0.0001,
            "vel_atm_ce_iv_delta_open": -0.005,
            "vel_atm_pe_iv_delta_open": 0.0001,
            "vel_pcr_delta_open": -0.147,
            "vel_pcr_acceleration": -0.076,
            "vel_pcr_trend_direction": -1.0,
            "ctx_gap_pct": 0.002,
            "ctx_gap_up": 1.0,
            "ctx_gap_down": 0.0,
            "ctx_am_gap_filled": 0.0,
        },
        "opening_range": {
            "opening_range_ready": 1, "or_width_pct": 0.0015,
            "price_vs_orh": 0.002, "price_vs_orl": 0.008,
            "orh_broken": 0, "orl_broken": 0,
        },
        "mtf_derived": {"rsi_14_1m": 52.0, "mtf_aligned": 1.0},
        "ladder_aggregates": {"near_atm_pcr": 0.95},
    })


def test_direction_features_resolved() -> None:
    """All direction model training features must resolve to non-NaN values
    from a realistic runtime snapshot using stage views + essential feature computation."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, _DIRECTION_FEATURES)
    assert row is not None, "build_feature_row returned None"

    nan_features = [
        f for f in _DIRECTION_FEATURES
        if f in row and not math.isfinite(row[f])
    ]
    assert not nan_features, (
        f"Direction features were NaN after extraction: {nan_features}. "
        "Train/serve skew — essential feature computation is missing a feature."
    )


def test_atm_iv_computed_from_ce_pe_average() -> None:
    """atm_iv must be computed as (atm_ce_iv + atm_pe_iv) / 2 when not directly available."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, ["atm_iv"])
    assert row is not None
    assert math.isfinite(row["atm_iv"]), "atm_iv was NaN"
    expected = (0.124 + 0.128) / 2.0
    assert abs(row["atm_iv"] - expected) < 1e-9, f"atm_iv={row['atm_iv']} != {expected}"


def test_vix_current_resolves() -> None:
    """Feature 'vix_current' must resolve from vix_context."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, ["vix_current"])
    assert row is not None
    assert math.isfinite(row["vix_current"]), "vix_current was NaN"
    assert abs(row["vix_current"] - 14.2) < 1e-9


def test_pcr_resolves_from_chain_aggregates() -> None:
    """Feature 'pcr' must resolve from chain_aggregates.pcr."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, ["pcr"])
    assert row is not None
    assert math.isfinite(row["pcr"]), "pcr was NaN"
    assert abs(row["pcr"] - 0.92) < 1e-9


def test_atm_ce_oi_resolves() -> None:
    """Feature 'atm_ce_oi' must resolve from atm_options."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, ["atm_ce_oi", "atm_pe_oi"])
    assert row is not None
    assert math.isfinite(row["atm_ce_oi"]), "atm_ce_oi was NaN"
    assert math.isfinite(row["atm_pe_oi"]), "atm_pe_oi was NaN"
    assert abs(row["atm_ce_oi"] - 125000.0) < 1e-3
    assert abs(row["atm_pe_oi"] - 118000.0) < 1e-3


def test_minute_of_day_computed_from_minutes_since_open() -> None:
    """minute_of_day = minutes_since_open + 555 (9:15 in minutes)."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, ["minute_of_day"])
    assert row is not None
    assert math.isfinite(row["minute_of_day"])
    # minutes_since_open=135 → 135 + 555 = 690 (11:30)
    assert abs(row["minute_of_day"] - 690.0) < 1e-9


def test_is_high_vix_day_computed() -> None:
    """is_high_vix_day must be 0 when vix_prev_close < 18.0."""
    snap = _make_direction_snap()
    row = build_feature_row(snap, ["is_high_vix_day"])
    assert row is not None
    assert math.isfinite(row["is_high_vix_day"])
    assert row["is_high_vix_day"] == 0.0  # vix_prev_close=13.8 < 18.0

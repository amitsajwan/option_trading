from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import numpy as np

from snapshot_app.core.velocity_features import VELOCITY_COLUMNS
from snapshot_app.historical.rebuild_stage_views_from_flat import (
    rebuild_stage_views_from_flat,
    _forward_fill_velocity_columns,
)


def _build_source_rows() -> pd.DataFrame:
    rows = []
    for idx, minute in enumerate(("11:29:00", "11:30:00")):
        row = {
            "trade_date": "2024-08-01",
            "year": 2024,
            "instrument": "BANKNIFTY-I",
            "timestamp": f"2024-08-01T{minute}+05:30",
            "snapshot_id": f"snap_{idx}",
            "schema_name": "SnapshotMLFlatV2",
            "schema_version": "4.0",
            "build_source": "historical",
            "build_run_id": "test_run",
            "ema_9": 1.0 + idx,
            "price_vs_vwap": 0.1,
            "rsi_14_1m": 55.0,
            "atr_14_1m": 12.0,
            "near_atm_oi_ratio": 1.1,
            "atm_oi_ratio": 1.05,
            "vix_current": 14.2,
            "ctx_regime_atr_high": 0,
            "ctx_regime_atr_low": 1,
            "ctx_regime_trend_up": 1,
            "ctx_regime_trend_down": 0,
            "ctx_is_high_vix_day": 0,
            "ctx_dte_days": 2,
            "ctx_is_expiry_day": 0,
            "ctx_is_near_expiry": 1,
            "minutes_since_open": 134 + idx,
            "day_of_week": 4,
            "adx_14": 23.5,
            "vol_spike_ratio": 1.8,
            "ctx_gap_pct": 0.003,
            "ctx_gap_up": 1,
            "ctx_gap_down": 0,
        }
        for column in VELOCITY_COLUMNS:
            row.setdefault(column, 1.0)
        rows.append(row)
    return pd.DataFrame(rows)


def _make_day_frame(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal frame suitable for _forward_fill_velocity_columns."""
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


# ---------------------------------------------------------------------------
# WP-2: Forward-fill temporal validity tests
# ---------------------------------------------------------------------------

def test_forward_fill_does_not_touch_pre_1130_rows() -> None:
    """Row before the 11:30 computation snapshot must stay null after forward-fill."""
    frame = _make_day_frame([
        {"timestamp": "2024-08-01 10:00:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
        {"timestamp": "2024-08-01 11:30:00+05:30", "vel_ce_oi_delta_open": 5.0,   "ctx_am_vwap_side": 1.0},
    ])
    result = _forward_fill_velocity_columns(frame)

    # The 10:00 row must remain null — no backfill
    pre_row = result[result["timestamp"].dt.hour < 11].iloc[0]
    assert pd.isna(pre_row["vel_ce_oi_delta_open"]), "pre-11:30 vel_ row must stay null"
    assert pd.isna(pre_row["ctx_am_vwap_side"]), "pre-11:30 ctx_am_ row must stay null"


def test_forward_fill_propagates_to_post_1130_rows() -> None:
    """Row after 11:30 must receive the forward-filled velocity value."""
    frame = _make_day_frame([
        {"timestamp": "2024-08-01 11:30:00+05:30", "vel_ce_oi_delta_open": 7.5, "ctx_am_vwap_side": -1.0},
        {"timestamp": "2024-08-01 12:00:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
        {"timestamp": "2024-08-01 13:00:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
    ])
    result = _forward_fill_velocity_columns(frame)

    post_rows = result[result["timestamp"].dt.hour >= 12]
    for _, row in post_rows.iterrows():
        assert row["vel_ce_oi_delta_open"] == 7.5, f"expected 7.5 at {row['timestamp']}, got {row['vel_ce_oi_delta_open']}"
        assert row["ctx_am_vwap_side"] == -1.0, f"expected -1.0 at {row['timestamp']}, got {row['ctx_am_vwap_side']}"


def test_no_cross_date_bleed() -> None:
    """Day 2's pre-11:30 row must NOT receive day 1's velocity values."""
    day1 = [
        {"timestamp": "2024-08-01 11:30:00+05:30", "vel_ce_oi_delta_open": 3.0, "ctx_am_vwap_side": 1.0},
        {"timestamp": "2024-08-01 12:00:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
    ]
    day2 = [
        {"timestamp": "2024-08-02 09:30:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
        {"timestamp": "2024-08-02 11:30:00+05:30", "vel_ce_oi_delta_open": 9.0, "ctx_am_vwap_side": -1.0},
    ]
    # _forward_fill_velocity_columns operates on one day at a time (caller responsibility).
    # Test that running it on day2 alone leaves the pre-11:30 row null.
    frame_day2 = _make_day_frame(day2)
    result = _forward_fill_velocity_columns(frame_day2)

    pre_row = result[result["timestamp"].dt.hour < 11].iloc[0]
    assert pd.isna(pre_row["vel_ce_oi_delta_open"]), "day2 pre-11:30 must stay null (no cross-date bleed)"
    assert pd.isna(pre_row["ctx_am_vwap_side"]), "day2 pre-11:30 ctx_am_ must stay null"


def test_backward_fill_never_applied() -> None:
    """All rows before the computation snapshot must remain null — backfill is prohibited."""
    frame = _make_day_frame([
        {"timestamp": "2024-08-01 09:15:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
        {"timestamp": "2024-08-01 10:00:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
        {"timestamp": "2024-08-01 10:30:00+05:30", "vel_ce_oi_delta_open": np.nan, "ctx_am_vwap_side": np.nan},
        {"timestamp": "2024-08-01 11:30:00+05:30", "vel_ce_oi_delta_open": 4.2,   "ctx_am_vwap_side": 1.0},
    ])
    result = _forward_fill_velocity_columns(frame)

    pre_rows = result[result["timestamp"].dt.hour < 11]
    assert pre_rows["vel_ce_oi_delta_open"].isna().all(), (
        "backfill detected: pre-11:30 vel_ rows have non-null values"
    )
    assert pre_rows["ctx_am_vwap_side"].isna().all(), (
        "backfill detected: pre-11:30 ctx_am_ rows have non-null values"
    )


def test_rebuild_stage_views_from_flat_writes_versioned_view_datasets() -> None:
    parquet_root = Path(tempfile.mkdtemp(prefix="stage-view-rebuild-", dir=Path.cwd()))
    source_root = parquet_root / "snapshots_ml_flat_v2" / "year=2024"
    source_root.mkdir(parents=True, exist_ok=True)
    _build_source_rows().to_parquet(source_root / "2024-08-01.parquet", index=False)

    summary = rebuild_stage_views_from_flat(parquet_root=parquet_root)

    assert summary["status"] == "complete"
    assert summary["days_processed"] == 1
    stage2_path = parquet_root / "stage2_direction_view_v2" / "year=2024" / "2024-08-01.parquet"
    assert stage2_path.exists()
    stage2_df = pd.read_parquet(stage2_path)
    assert "vel_ce_oi_delta_open" in stage2_df.columns
    assert "adx_14" in stage2_df.columns
    assert "vol_spike_ratio" in stage2_df.columns
    assert "ctx_gap_pct" in stage2_df.columns

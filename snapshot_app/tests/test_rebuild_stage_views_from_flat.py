from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from snapshot_app.core.velocity_features import VELOCITY_COLUMNS
from snapshot_app.historical.rebuild_stage_views_from_flat import rebuild_stage_views_from_flat


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

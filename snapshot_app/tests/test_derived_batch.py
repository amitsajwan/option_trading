from __future__ import annotations

from pathlib import Path

import pandas as pd

from snapshot_app.historical.derived_batch import run_derived_batch


def test_run_derived_batch_projects_market_base_to_ml_flat_and_stage_views(tmp_path: Path) -> None:
    market_base_path = tmp_path / "market_base" / "year=2020" / "chunk=202001_202001_m1"
    market_base_path.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "trade_date": "2020-01-30",
                "year": 2020,
                "timestamp": "2020-01-30T09:15:00+05:30",
                "snapshot_id": "20200130_0915",
                "schema_name": "MarketSnapshot",
                "schema_version": "3.0",
                "instrument": "BANKNIFTY-I",
                "build_source": "historical",
                "build_run_id": "test_run",
                "opt_flow_rows": 3.0,
                "minutes_since_open": 0.0,
                "vix_current": 14.0,
            }
        ]
    ).to_parquet(market_base_path / "data.parquet", index=False)

    result = run_derived_batch(
        parquet_base=tmp_path,
        min_day="2020-01-30",
        max_day="2020-01-30",
        resume=False,
        write_batch_days=1,
        build_source="historical",
        build_run_id="test_run",
        validate_ml_flat_contract=False,
        partition_key="202001_202001_m1",
    )

    ml_flat_path = tmp_path / "snapshots_ml_flat" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    stage1_path = tmp_path / "stage1_entry_view" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    stage2_path = tmp_path / "stage2_direction_view" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    stage3_path = tmp_path / "stage3_recipe_view" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"

    assert result["status"] == "complete"
    assert result["days_processed"] == 1
    assert result["total_rows"] == 1
    assert ml_flat_path.exists()
    assert stage1_path.exists()
    assert stage2_path.exists()
    assert stage3_path.exists()
    assert not list(ml_flat_path.parent.glob("data.tmp_*.parquet"))
    assert not list(stage1_path.parent.glob("data.tmp_*.parquet"))
    assert not list(stage2_path.parent.glob("data.tmp_*.parquet"))
    assert not list(stage3_path.parent.glob("data.tmp_*.parquet"))

    ml_flat_df = pd.read_parquet(ml_flat_path)
    stage1_df = pd.read_parquet(stage1_path)
    stage2_df = pd.read_parquet(stage2_path)
    stage3_df = pd.read_parquet(stage3_path)

    assert ml_flat_df["trade_date"].astype(str).tolist() == ["2020-01-30"]
    assert stage1_df["view_name"].tolist() == ["stage1_entry_view"]
    assert stage2_df["view_name"].tolist() == ["stage2_direction_view"]
    assert stage3_df["view_name"].tolist() == ["stage3_recipe_view"]
    assert {"pcr_change_5m", "pcr_change_15m", "atm_ce_oi", "atm_pe_oi", "atm_oi_ratio", "near_atm_oi_ratio"} <= set(stage2_df.columns)

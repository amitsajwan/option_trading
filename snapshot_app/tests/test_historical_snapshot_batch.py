from __future__ import annotations

from pathlib import Path

import pandas as pd

from snapshot_app.historical.snapshot_batch import run_snapshot_batch


class _FakeParquetStore:
    def __init__(self, base_path: Path, *, snapshots_dataset: str = "snapshots") -> None:
        self.base_path = Path(base_path)
        self.snapshots_dataset = snapshots_dataset

    def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return ["2020-01-30"]

    def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return []

    def has_options_for_day(self, trade_date: str) -> bool:
        return True

    def vix(self) -> pd.DataFrame:
        return pd.DataFrame()


def test_run_snapshot_batch_flushes_canonical_and_ml_flat(monkeypatch, tmp_path: Path) -> None:
    def _fake_process_day(**kwargs):
        trade_date = str(kwargs["trade_date"])
        return {
            "snapshot_rows": [
                {
                    "trade_date": trade_date,
                    "timestamp": f"{trade_date} 09:15:00",
                    "snapshot_id": f"{trade_date}_001",
                    "snapshot_raw_json": "{}",
                    "build_source": "historical",
                    "build_run_id": "test_run",
                }
            ],
            "ml_flat_rows": [
                {
                    "trade_date": trade_date,
                    "timestamp": f"{trade_date} 09:15:00",
                    "snapshot_id": f"{trade_date}_001",
                    "schema_name": "SnapshotMLFlat",
                    "schema_version": "3.0",
                    "build_source": "historical",
                    "build_run_id": "test_run",
                    "opt_flow_rows": 3,
                }
            ],
            "stage_rows": {
                "stage1_entry_view": [
                    {
                        "trade_date": trade_date,
                        "year": 2020,
                        "timestamp": f"{trade_date} 09:15:00",
                        "snapshot_id": f"{trade_date}_001",
                        "build_source": "historical",
                        "build_run_id": "test_run",
                        "view_name": "stage1_entry_view",
                    }
                ],
                "stage2_direction_view": [
                    {
                        "trade_date": trade_date,
                        "year": 2020,
                        "timestamp": f"{trade_date} 09:15:00",
                        "snapshot_id": f"{trade_date}_001",
                        "build_source": "historical",
                        "build_run_id": "test_run",
                        "view_name": "stage2_direction_view",
                    }
                ],
                "stage3_recipe_view": [
                    {
                        "trade_date": trade_date,
                        "year": 2020,
                        "timestamp": f"{trade_date} 09:15:00",
                        "snapshot_id": f"{trade_date}_001",
                        "build_source": "historical",
                        "build_run_id": "test_run",
                        "view_name": "stage3_recipe_view",
                    }
                ],
            },
        }

    monkeypatch.setattr("snapshot_app.historical.snapshot_batch.ParquetStore", _FakeParquetStore)
    monkeypatch.setattr("snapshot_app.historical.snapshot_batch.process_day", _fake_process_day)

    result = run_snapshot_batch(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        resume=True,
        write_batch_days=1,
        build_source="historical",
        build_run_id="test_run",
        validate_ml_flat_contract=False,
    )

    snapshots_path = tmp_path / "snapshots" / "year=2020" / "data.parquet"
    ml_flat_path = tmp_path / "snapshots_ml_flat" / "year=2020" / "data.parquet"
    stage1_path = tmp_path / "stage1_entry_view" / "year=2020" / "data.parquet"
    stage2_path = tmp_path / "stage2_direction_view" / "year=2020" / "data.parquet"
    stage3_path = tmp_path / "stage3_recipe_view" / "year=2020" / "data.parquet"

    assert result["status"] == "complete"
    assert result["days_processed"] == 1
    assert result["total_snapshot_rows"] == 1
    assert result["total_rows"] == 1
    assert snapshots_path.exists()
    assert ml_flat_path.exists()
    assert stage1_path.exists()
    assert stage2_path.exists()
    assert stage3_path.exists()

    snapshots_df = pd.read_parquet(snapshots_path)
    ml_flat_df = pd.read_parquet(ml_flat_path)
    stage1_df = pd.read_parquet(stage1_path)
    stage2_df = pd.read_parquet(stage2_path)
    stage3_df = pd.read_parquet(stage3_path)

    assert snapshots_df["trade_date"].astype(str).tolist() == ["2020-01-30"]
    assert ml_flat_df["trade_date"].astype(str).tolist() == ["2020-01-30"]
    assert stage1_df["view_name"].tolist() == ["stage1_entry_view"]
    assert stage2_df["view_name"].tolist() == ["stage2_direction_view"]
    assert stage3_df["view_name"].tolist() == ["stage3_recipe_view"]

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

    assert result["status"] == "complete"
    assert result["days_processed"] == 1
    assert result["total_snapshot_rows"] == 1
    assert result["total_rows"] == 1
    assert snapshots_path.exists()
    assert ml_flat_path.exists()

    snapshots_df = pd.read_parquet(snapshots_path)
    ml_flat_df = pd.read_parquet(ml_flat_path)

    assert snapshots_df["trade_date"].astype(str).tolist() == ["2020-01-30"]
    assert ml_flat_df["trade_date"].astype(str).tolist() == ["2020-01-30"]

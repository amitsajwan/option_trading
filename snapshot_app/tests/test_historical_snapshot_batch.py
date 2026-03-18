from __future__ import annotations

from pathlib import Path

import pandas as pd

from snapshot_app.historical.snapshot_batch import _project_rows_to_ml_flat, run_snapshot_batch


class _FakeParquetStore:
    def __init__(self, base_path: Path, *, snapshots_dataset: str = "snapshots") -> None:
        self.base_path = Path(base_path)
        self.snapshots_dataset = snapshots_dataset
        self.has_options_calls = 0

    def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return ["2020-01-29", "2020-01-30"]

    def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return []

    def has_options_for_day(self, trade_date: str) -> bool:
        self.has_options_calls += 1
        return True

    def all_days_with_options(self, *, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return ["2020-01-29", "2020-01-30"]

    def vix(self) -> pd.DataFrame:
        return pd.DataFrame()


def test_run_snapshot_batch_flushes_canonical_and_ml_flat(monkeypatch, tmp_path: Path) -> None:
    emit_calls: list[tuple[str, bool]] = []
    futures_window_calls: list[tuple[str, list[str] | None]] = []

    def _fake_process_day(**kwargs):
        trade_date = str(kwargs["trade_date"])
        emit_calls.append((trade_date, bool(kwargs.get("emit_outputs", True))))
        futures_window_calls.append((trade_date, list(kwargs.get("futures_window_days") or [])))
        if not bool(kwargs.get("emit_outputs", True)):
            return {
                "snapshot_rows": [],
                "ml_flat_rows": [],
                "stage_rows": {
                    "stage1_entry_view": [],
                    "stage2_direction_view": [],
                    "stage3_recipe_view": [],
                },
            }
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
        min_day="2020-01-30",
        max_day="2020-01-30",
        planned_days=["2020-01-29", "2020-01-30"],
        emit_days=["2020-01-30"],
        resume=False,
        write_batch_days=1,
        build_source="historical",
        build_run_id="test_run",
        validate_ml_flat_contract=False,
        partition_key="202001_202001_m1",
    )

    snapshots_path = tmp_path / "snapshots" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    ml_flat_path = tmp_path / "snapshots_ml_flat" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    stage1_path = tmp_path / "stage1_entry_view" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    stage2_path = tmp_path / "stage2_direction_view" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    stage3_path = tmp_path / "stage3_recipe_view" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"

    assert result["status"] == "complete"
    assert result["days_processed"] == 1
    assert result["warmup_days_processed"] == 1
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
    assert emit_calls == [("2020-01-29", False), ("2020-01-30", True)]
    assert futures_window_calls == [
        ("2020-01-29", ["2020-01-29"]),
        ("2020-01-30", ["2020-01-29", "2020-01-30"]),
    ]


def test_project_rows_to_ml_flat_prefers_canonical_same_strike_atm_fields() -> None:
    rows = [
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:15:00+05:30",
            "snapshot_id": "20200130_0915",
            "atm_ce_close": 100.0,
            "atm_pe_close": 90.0,
            "atm_ce_oi": 1000.0,
            "atm_pe_oi": 900.0,
            "opt_flow_atm_strike": 50000.0,
            "opt_flow_rows": 3.0,
        },
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:16:00+05:30",
            "snapshot_id": "20200130_0916",
            "atm_ce_close": 80.0,
            "atm_pe_close": 110.0,
            "atm_ce_oi": 1015.0,
            "atm_pe_oi": 920.0,
            "atm_ce_return_1m": 0.123,
            "atm_pe_return_1m": -0.234,
            "atm_ce_oi_change_1m": 10.0,
            "atm_pe_oi_change_1m": 20.0,
            "opt_flow_atm_strike": 50100.0,
            "opt_flow_rows": 3.0,
        },
    ]

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert projected[1]["opt_flow_atm_call_return_1m"] == 0.123
    assert projected[1]["opt_flow_atm_put_return_1m"] == -0.234
    assert projected[1]["opt_flow_atm_oi_change_1m"] == 30.0

from __future__ import annotations

from pathlib import Path

from snapshot_app.pipeline.orchestrator import run_snapshot_builds


class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _FakeExecutor:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, payload):
        return _FakeFuture(fn(payload))


class _FakeStore:
    def __init__(self, base_path: Path, *, snapshots_dataset: str = "snapshots_ml_flat") -> None:
        self.base_path = Path(base_path)
        self.snapshots_dataset = snapshots_dataset

    def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return [
            "2023-12-29",
            "2024-01-01",
            "2024-01-02",
        ]


def test_run_snapshot_builds_parallel_year_slices_aggregates(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def _fake_run_snapshot_batch(**kwargs):
        calls.append((kwargs.get("min_day"), kwargs.get("max_day")))
        return {
            "status": "complete",
            "days_available": 1,
            "days_pending": 1,
            "days_processed": 1,
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": 0,
            "days_no_rows": 0,
            "error_count": 0,
            "error_days": [],
            "total_rows": 10,
            "iv_diagnostics": {"minutes": 10, "ce_iv_non_null": 5},
            "iv_diagnostics_days_with_failures": [],
            "elapsed_sec": 1.5,
        }

    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ParquetStore", _FakeStore)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.run_snapshot_batch", _fake_run_snapshot_batch)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ProcessPoolExecutor", _FakeExecutor)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.as_completed", lambda futures: list(futures))

    result = run_snapshot_builds(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        snapshot_jobs=2,
    )

    assert result["status"] == "complete"
    assert result["parallel_year_slices"] == 2
    assert result["days_processed"] == 2
    assert result["total_rows"] == 20
    assert result["iv_diagnostics"]["minutes"] == 20
    assert sorted(calls) == [("2023-12-29", "2023-12-29"), ("2024-01-01", "2024-01-02")]

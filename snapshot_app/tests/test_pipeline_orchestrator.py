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

    def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return []

    def all_days_with_options(self, *, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return self.available_days(min_day=min_day, max_day=max_day)


def test_run_snapshot_builds_parallel_slices_aggregate_results(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def _fake_run_snapshot_batch(**kwargs):
        calls.append(
            {
                "min_day": kwargs.get("min_day"),
                "max_day": kwargs.get("max_day"),
                "planned_days": list(kwargs.get("planned_days") or []),
                "emit_days": list(kwargs.get("emit_days") or kwargs.get("explicit_days") or []),
                "partition_key": kwargs.get("partition_key"),
            }
        )
        emitted = list(kwargs.get("emit_days") or kwargs.get("explicit_days") or [])
        warmup = list(kwargs.get("planned_days") or [])
        return {
            "status": "complete",
            "days_available": len(emitted),
            "days_pending": len(emitted),
            "days_processed": len(emitted),
            "warmup_days_processed": max(0, len(warmup) - len(emitted)),
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": 0,
            "missing_input_days": [],
            "days_no_rows": 0,
            "no_row_days": [],
            "error_count": 0,
            "error_days": [],
            "total_rows": 10 * len(emitted),
            "total_snapshot_rows": 10 * len(emitted),
            "iv_diagnostics": {"minutes": 10 * len(emitted), "ce_iv_non_null": 5 * len(emitted)},
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
        slice_months=6,
        slice_warmup_days=1,
        build_stage="snapshots",
    )

    assert result["status"] == "complete"
    assert result["parallel_slices"] == 2
    assert result["days_processed"] == 3
    assert result["warmup_days_processed"] == 1
    assert result["total_rows"] == 30
    assert result["iv_diagnostics"]["minutes"] == 30
    assert len(calls) == 2
    assert calls[0]["planned_days"] == ["2023-12-29"]
    assert calls[0]["emit_days"] == ["2023-12-29"]
    assert calls[1]["planned_days"] == ["2023-12-29", "2024-01-01", "2024-01-02"]
    assert calls[1]["emit_days"] == ["2024-01-01", "2024-01-02"]


def test_run_snapshot_builds_sparse_explicit_days_keep_internal_continuity(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class _SparseStore(_FakeStore):
        def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
            return [
                "2024-01-01",
                "2024-01-02",
                "2024-01-03",
            ]

    def _fake_run_snapshot_batch(**kwargs):
        calls.append(
            {
                "planned_days": list(kwargs.get("planned_days") or []),
                "emit_days": list(kwargs.get("emit_days") or []),
            }
        )
        return {
            "status": "complete",
            "days_available": len(kwargs.get("emit_days") or []),
            "days_pending": len(kwargs.get("emit_days") or []),
            "days_processed": len(kwargs.get("emit_days") or []),
            "warmup_days_processed": max(0, len(kwargs.get("planned_days") or []) - len(kwargs.get("emit_days") or [])),
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": 0,
            "missing_input_days": [],
            "days_no_rows": 0,
            "no_row_days": [],
            "error_count": 0,
            "error_days": [],
            "total_rows": 10,
            "total_snapshot_rows": 10,
            "iv_diagnostics": {"minutes": 10},
            "iv_diagnostics_days_with_failures": [],
            "elapsed_sec": 1.0,
        }

    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ParquetStore", _SparseStore)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.run_snapshot_batch", _fake_run_snapshot_batch)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ProcessPoolExecutor", _FakeExecutor)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.as_completed", lambda futures: list(futures))

    run_snapshot_builds(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        explicit_days=["2024-01-01", "2024-01-03"],
        snapshot_jobs=2,
        slice_months=1,
        build_stage="snapshots",
        slice_warmup_days=0,
    )

    assert len(calls) == 1
    assert calls[0]["planned_days"] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert calls[0]["emit_days"] == ["2024-01-01", "2024-01-03"]


def test_run_snapshot_builds_propagates_partial_incomplete_status(monkeypatch, tmp_path: Path) -> None:
    def _fake_run_snapshot_batch(**kwargs):
        emitted = list(kwargs.get("emit_days") or kwargs.get("explicit_days") or [])
        first_day = str(emitted[0]) if emitted else ""
        status = "partial_incomplete" if first_day.startswith("2023-12") else "complete"
        missing_days = emitted[:1] if status == "partial_incomplete" else []
        return {
            "status": status,
            "days_available": len(emitted),
            "days_pending": len(emitted),
            "days_processed": len(emitted),
            "warmup_days_processed": 0,
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": len(missing_days),
            "missing_input_days": missing_days,
            "days_no_rows": 0,
            "no_row_days": [],
            "error_count": 0,
            "error_days": [],
            "total_rows": 10 * len(emitted),
            "total_snapshot_rows": 10 * len(emitted),
            "iv_diagnostics": {"minutes": 10 * len(emitted)},
            "iv_diagnostics_days_with_failures": [],
            "elapsed_sec": 1.0,
        }

    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ParquetStore", _FakeStore)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.run_snapshot_batch", _fake_run_snapshot_batch)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ProcessPoolExecutor", _FakeExecutor)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.as_completed", lambda futures: list(futures))

    result = run_snapshot_builds(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        snapshot_jobs=2,
        slice_months=6,
        slice_warmup_days=1,
        build_stage="snapshots",
    )

    assert result["status"] == "partial_incomplete"
    assert result["days_skipped_missing_inputs"] == 1
    assert result["missing_input_days"] == ["2023-12-29"]


def test_run_snapshot_builds_filters_out_days_without_options_before_slicing(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class _OptionsGapStore(_FakeStore):
        def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
            return ["2024-01-01", "2024-01-02", "2024-01-03"]

        def all_days_with_options(self, *, min_day: str | None = None, max_day: str | None = None) -> list[str]:
            return ["2024-01-01", "2024-01-03"]

    def _fake_run_snapshot_batch(**kwargs):
        calls.append(
            {
                "planned_days": list(kwargs.get("planned_days") or []),
                "emit_days": list(kwargs.get("emit_days") or kwargs.get("explicit_days") or []),
            }
        )
        emitted = list(kwargs.get("emit_days") or kwargs.get("explicit_days") or [])
        return {
            "status": "complete",
            "days_available": len(emitted),
            "days_pending": len(emitted),
            "days_processed": len(emitted),
            "warmup_days_processed": 0,
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": 0,
            "missing_input_days": [],
            "days_no_rows": 0,
            "no_row_days": [],
            "error_count": 0,
            "error_days": [],
            "total_rows": 10 * len(emitted),
            "total_snapshot_rows": 10 * len(emitted),
            "iv_diagnostics": {"minutes": 10 * len(emitted)},
            "iv_diagnostics_days_with_failures": [],
            "elapsed_sec": 1.0,
        }

    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ParquetStore", _OptionsGapStore)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.run_snapshot_batch", _fake_run_snapshot_batch)

    result = run_snapshot_builds(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        snapshot_jobs=1,
        build_stage="snapshots",
    )

    assert result["status"] == "complete"
    assert result["days_available"] == 2
    assert result["days_processed"] == 2
    assert calls == [
        {
            "planned_days": [],
            "emit_days": ["2024-01-01", "2024-01-03"],
        }
    ]


def test_run_snapshot_builds_all_stage_aggregates_snapshots_and_derived(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, list[str]]] = []

    class _AllStageStore(_FakeStore):
        def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
            if self.snapshots_dataset == "market_base":
                return ["2024-01-01", "2024-01-02"]
            return []

        def all_days_with_options(self, *, min_day: str | None = None, max_day: str | None = None) -> list[str]:
            return ["2024-01-01", "2024-01-02"]

    def _fake_run_snapshot_batch(**kwargs):
        emitted = list(kwargs.get("emit_days") or kwargs.get("explicit_days") or [])
        calls.append(("snapshots", emitted))
        return {
            "status": "complete",
            "days_available": len(emitted),
            "days_pending": len(emitted),
            "days_processed": len(emitted),
            "warmup_days_processed": 0,
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": 0,
            "missing_input_days": [],
            "days_no_rows": 0,
            "no_row_days": [],
            "error_count": 0,
            "error_days": [],
            "total_rows": 10 * len(emitted),
            "total_snapshot_rows": 10 * len(emitted),
            "total_market_base_rows": 10 * len(emitted),
            "iv_diagnostics": {"minutes": 10 * len(emitted)},
            "iv_diagnostics_days_with_failures": [],
            "elapsed_sec": 1.0,
        }

    def _fake_run_derived_batch(**kwargs):
        emitted = list(kwargs.get("emit_days") or kwargs.get("explicit_days") or [])
        calls.append(("derived", emitted))
        return {
            "status": "complete",
            "days_available": len(emitted),
            "days_pending": len(emitted),
            "days_processed": len(emitted),
            "warmup_days_processed": 0,
            "days_skipped_existing": 0,
            "days_skipped_missing_inputs": 0,
            "missing_input_days": [],
            "days_no_rows": 0,
            "no_row_days": [],
            "error_count": 0,
            "error_days": [],
            "total_rows": 5 * len(emitted),
            "total_snapshot_rows": 0,
            "total_market_base_rows": 0,
            "iv_diagnostics": {},
            "iv_diagnostics_days_with_failures": [],
            "elapsed_sec": 0.5,
        }

    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.ParquetStore", _AllStageStore)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.run_snapshot_batch", _fake_run_snapshot_batch)
    monkeypatch.setattr("snapshot_app.pipeline.orchestrator.run_derived_batch", _fake_run_derived_batch)

    result = run_snapshot_builds(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        snapshot_jobs=1,
        build_stage="all",
    )

    assert result["status"] == "complete"
    assert result["days_processed"] == 2
    assert result["total_snapshot_rows"] == 20
    assert result["total_market_base_rows"] == 20
    assert result["total_rows"] == 10
    assert ("snapshots", ["2024-01-01", "2024-01-02"]) in calls
    assert ("derived", ["2024-01-01", "2024-01-02"]) in calls

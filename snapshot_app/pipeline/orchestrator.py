from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_batch import (
    OUTPUT_DATASET_ML_FLAT,
    OUTPUT_DATASET_SNAPSHOTS,
    STAGE_OUTPUT_DATASETS,
    run_snapshot_batch,
)

from .config import (
    DEFAULT_NORMALIZE_JOBS,
    DEFAULT_PARQUET_BASE,
    DEFAULT_RAW_DATA_ROOT,
    DEFAULT_SNAPSHOT_JOBS,
)
from .normalize import normalize_raw_to_parquet


DEFAULT_SLICE_MONTHS = 6
DEFAULT_SLICE_WARMUP_DAYS = 90


def _output_datasets() -> tuple[str, ...]:
    return (
        OUTPUT_DATASET_SNAPSHOTS,
        OUTPUT_DATASET_ML_FLAT,
        *STAGE_OUTPUT_DATASETS,
    )


def _available_target_days(
    *,
    store: ParquetStore,
    min_day: str | None,
    max_day: str | None,
    explicit_days: Optional[Sequence[str]],
) -> list[str]:
    days = store.available_days(min_day=min_day, max_day=max_day)
    if not explicit_days:
        return [str(day) for day in days]
    requested = {str(day) for day in explicit_days if str(day).strip()}
    return [str(day) for day in days if str(day) in requested]


def _completed_output_days(
    *,
    parquet_base: Path,
    min_day: str | None,
    max_day: str | None,
    requested_days: set[str],
) -> set[str]:
    day_sets: list[set[str]] = []
    for dataset_name in _output_datasets():
        dataset_store = ParquetStore(parquet_base, snapshots_dataset=dataset_name)
        days = set(dataset_store.available_snapshot_days(min_day=min_day, max_day=max_day))
        day_sets.append(days.intersection(requested_days))
    if not day_sets:
        return set()
    return set.intersection(*day_sets)


def _has_legacy_year_layout(parquet_base: Path) -> bool:
    for dataset_name in _output_datasets():
        root = parquet_base / dataset_name
        if next(root.glob("year=*/data.parquet"), None) is not None:
            return True
    return False


def _slice_anchor(day: str, *, slice_months: int) -> tuple[int, int]:
    ts = pd.Timestamp(day)
    start_month = ((int(ts.month) - 1) // int(slice_months)) * int(slice_months) + 1
    return int(ts.year), int(start_month)


def _partition_key(target_days: Sequence[str], *, slice_months: int) -> str:
    first = pd.Timestamp(target_days[0])
    last = pd.Timestamp(target_days[-1])
    return (
        f"{int(first.year):04d}{int(first.month):02d}"
        f"_{int(last.year):04d}{int(last.month):02d}"
        f"_m{int(slice_months)}"
    )


def _build_parallel_slices(
    *,
    history_days: Sequence[str],
    target_days: Sequence[str],
    slice_months: int,
    warmup_days: int,
) -> list[dict[str, Any]]:
    if not target_days:
        return []
    history_index = {str(day): idx for idx, day in enumerate(history_days)}
    grouped: dict[tuple[int, int], list[str]] = {}
    for day in target_days:
        grouped.setdefault(_slice_anchor(day, slice_months=slice_months), []).append(str(day))

    slices: list[dict[str, Any]] = []
    for (_, _), days in sorted(grouped.items(), key=lambda item: item[1][0]):
        first_idx = history_index[str(days[0])]
        last_idx = history_index[str(days[-1])]
        warmup_start = max(0, int(first_idx - max(0, int(warmup_days))))
        planned = [str(day) for day in history_days[warmup_start : last_idx + 1]]
        slices.append(
            {
                "min_day": str(days[0]),
                "max_day": str(days[-1]),
                "emit_days": [str(day) for day in days],
                "planned_days": planned,
                "warmup_days": int(len(planned) - len(days)),
                "partition_key": _partition_key(days, slice_months=slice_months),
            }
        )
    return slices

def _merge_iv_diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for row in rows:
        payload = row.get("iv_diagnostics")
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            merged[key] = int(merged.get(key, 0)) + int(value or 0)
    return merged


def _build_slice_entry(payload: dict[str, Any]) -> dict[str, Any]:
    return run_snapshot_batch(**payload)


def run_snapshot_builds(
    *,
    parquet_base: str | Path = DEFAULT_PARQUET_BASE,
    instrument: str,
    min_day: str | None = None,
    max_day: str | None = None,
    explicit_days: Optional[Sequence[str]] = None,
    lookback_days: int = 30,
    resume: bool = True,
    dry_run: bool = False,
    log_every: int = 10,
    write_batch_days: int = 20,
    output_dataset: str = "snapshots_ml_flat",
    build_source: str = "historical",
    build_run_id: str | None = None,
    validate_ml_flat_contract: bool = False,
    snapshot_jobs: int = DEFAULT_SNAPSHOT_JOBS,
    slice_months: int = DEFAULT_SLICE_MONTHS,
    slice_warmup_days: int = DEFAULT_SLICE_WARMUP_DAYS,
) -> dict[str, Any]:
    resolved_base = Path(parquet_base)
    store = ParquetStore(resolved_base, snapshots_dataset=output_dataset)
    target_days = _available_target_days(
        store=store,
        min_day=min_day,
        max_day=max_day,
        explicit_days=explicit_days,
    )
    if not target_days:
        return {
            "status": "no_days",
            "output_dataset": output_dataset,
            "days_available": 0,
        }

    completed_days = (
        _completed_output_days(
            parquet_base=resolved_base,
            min_day=min_day,
            max_day=max_day,
            requested_days=set(target_days),
        )
        if resume
        else set()
    )
    pending_target_days = [day for day in target_days if day not in completed_days]
    if not pending_target_days:
        return {
            "status": "already_complete",
            "output_dataset": output_dataset,
            "days_available": len(target_days),
            "days_skipped_existing": len(completed_days),
            "days_pending": 0,
        }

    if int(snapshot_jobs) <= 1:
        return run_snapshot_batch(
            parquet_base=resolved_base,
            instrument=instrument,
            min_day=min_day,
            max_day=max_day,
            explicit_days=pending_target_days,
            lookback_days=lookback_days,
            resume=False,
            dry_run=dry_run,
            log_every=log_every,
            write_batch_days=write_batch_days,
            output_dataset=output_dataset,
            build_source=build_source,
            build_run_id=build_run_id,
            validate_ml_flat_contract=validate_ml_flat_contract,
        )

    if _has_legacy_year_layout(resolved_base):
        raise RuntimeError(
            "parallel chunk snapshot builds require a clean snapshot output root. "
            "Found legacy yearly parquet files under snapshots/snapshots_ml_flat/stage views. "
            "Delete those datasets and rerun the build."
        )

    history_max_day = max(pending_target_days)
    history_days = store.available_days(min_day=None, max_day=history_max_day)
    slices = _build_parallel_slices(
        history_days=history_days,
        target_days=pending_target_days,
        slice_months=max(1, int(slice_months)),
        warmup_days=max(0, int(slice_warmup_days)),
    )
    if len(slices) <= 1:
        only = slices[0]
        return run_snapshot_batch(
            parquet_base=resolved_base,
            instrument=instrument,
            min_day=str(only["min_day"]),
            max_day=str(only["max_day"]),
            planned_days=list(only["planned_days"]),
            emit_days=list(only["emit_days"]),
            lookback_days=lookback_days,
            resume=False,
            dry_run=dry_run,
            log_every=log_every,
            write_batch_days=write_batch_days,
            output_dataset=output_dataset,
            build_source=build_source,
            build_run_id=build_run_id,
            validate_ml_flat_contract=validate_ml_flat_contract,
            partition_key=str(only["partition_key"]),
        )

    payloads = [
        {
            "parquet_base": resolved_base,
            "instrument": instrument,
            "min_day": str(partition["min_day"]),
            "max_day": str(partition["max_day"]),
            "planned_days": list(partition["planned_days"]),
            "emit_days": list(partition["emit_days"]),
            "lookback_days": lookback_days,
            "resume": False,
            "dry_run": dry_run,
            "log_every": log_every,
            "write_batch_days": write_batch_days,
            "output_dataset": output_dataset,
            "build_source": build_source,
            "build_run_id": build_run_id,
            "validate_ml_flat_contract": validate_ml_flat_contract,
            "partition_key": str(partition["partition_key"]),
        }
        for partition in slices
    ]

    slice_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    max_workers = max(1, min(int(snapshot_jobs), len(payloads)))
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_build_slice_entry, payload): payload for payload in payloads}
        for future in as_completed(futures):
            payload = futures[future]
            try:
                result = future.result()
                result["slice"] = {
                    "min_day": str(payload.get("min_day") or ""),
                    "max_day": str(payload.get("max_day") or ""),
                    "warmup_days": int(len(payload.get("planned_days") or []) - len(payload.get("emit_days") or [])),
                    "partition_key": str(payload.get("partition_key") or ""),
                }
                slice_results.append(result)
            except Exception as exc:
                errors.append(
                    {
                        "min_day": str(payload.get("min_day") or ""),
                        "max_day": str(payload.get("max_day") or ""),
                        "partition_key": str(payload.get("partition_key") or ""),
                        "error": str(exc),
                    }
                )

    if errors:
        return {
            "status": "partial_error",
            "output_dataset": output_dataset,
            "build_source": build_source,
            "build_run_id": build_run_id,
            "parallel_slices": len(payloads),
            "parallel_slice_months": int(slice_months),
            "parallel_slice_warmup_days": int(slice_warmup_days),
            "slice_results": slice_results,
            "errors": errors,
        }

    statuses = {str(row.get("status") or "") for row in slice_results}
    status = "complete"
    if statuses == {"dry_run"}:
        status = "dry_run"
    elif statuses == {"already_complete"}:
        status = "already_complete"
    elif "no_days" in statuses and len(statuses) == 1:
        status = "no_days"

    return {
        "status": status,
        "output_dataset": output_dataset,
        "build_source": build_source,
        "build_run_id": build_run_id,
        "contract_validation_enabled": bool(validate_ml_flat_contract),
        "parallel_slices": len(payloads),
        "parallel_slice_months": int(slice_months),
        "parallel_slice_warmup_days": int(slice_warmup_days),
        "days_available": int(len(target_days)),
        "days_pending": int(len(pending_target_days)),
        "days_processed": int(sum(int(row.get("days_processed") or 0) for row in slice_results)),
        "warmup_days_processed": int(sum(int(row.get("warmup_days_processed") or 0) for row in slice_results)),
        "days_skipped_existing": int(len(completed_days)),
        "days_skipped_missing_inputs": int(sum(int(row.get("days_skipped_missing_inputs") or 0) for row in slice_results)),
        "days_no_rows": int(sum(int(row.get("days_no_rows") or 0) for row in slice_results)),
        "error_count": int(sum(int(row.get("error_count") or 0) for row in slice_results)),
        "error_days": [
            day
            for row in slice_results
            for day in list(row.get("error_days") or [])
        ],
        "total_rows": int(sum(int(row.get("total_rows") or 0) for row in slice_results)),
        "total_snapshot_rows": int(sum(int(row.get("total_snapshot_rows") or 0) for row in slice_results)),
        "iv_diagnostics": _merge_iv_diagnostics(slice_results),
        "iv_diagnostics_days_with_failures": [
            item
            for row in slice_results
            for item in list(row.get("iv_diagnostics_days_with_failures") or [])
        ],
        "elapsed_sec": float(sum(float(row.get("elapsed_sec") or 0.0) for row in slice_results)),
        "slice_results": sorted(
            slice_results,
            key=lambda row: (
                str((row.get("slice") or {}).get("min_day") or ""),
                str((row.get("slice") or {}).get("max_day") or ""),
            ),
        ),
    }


def run_snapshot_pipeline(
    *,
    raw_root: str | Path = DEFAULT_RAW_DATA_ROOT,
    parquet_base: str | Path = DEFAULT_PARQUET_BASE,
    vix_root: str | Path | None = None,
    normalize_jobs: int = DEFAULT_NORMALIZE_JOBS,
    snapshot_jobs: int = DEFAULT_SNAPSHOT_JOBS,
    force_normalize: bool = False,
    normalize_only: bool = False,
    instrument: str,
    min_day: str | None = None,
    max_day: str | None = None,
    explicit_days: Optional[Sequence[str]] = None,
    lookback_days: int = 30,
    resume: bool = True,
    dry_run: bool = False,
    log_every: int = 10,
    write_batch_days: int = 20,
    output_dataset: str = "snapshots_ml_flat",
    build_source: str = "historical",
    build_run_id: str | None = None,
    validate_ml_flat_contract: bool = False,
) -> dict[str, Any]:
    normalization = normalize_raw_to_parquet(
        raw_root=raw_root,
        parquet_base=parquet_base,
        vix_root=vix_root,
        jobs=normalize_jobs,
        force=force_normalize,
    )
    if normalize_only:
        return {
            "status": normalization.get("status"),
            "normalization": normalization,
        }

    build = run_snapshot_builds(
        parquet_base=parquet_base,
        instrument=instrument,
        min_day=min_day,
        max_day=max_day,
        explicit_days=explicit_days,
        lookback_days=lookback_days,
        resume=resume,
        dry_run=dry_run,
        log_every=log_every,
        write_batch_days=write_batch_days,
        output_dataset=output_dataset,
        build_source=build_source,
        build_run_id=build_run_id,
        validate_ml_flat_contract=validate_ml_flat_contract,
        snapshot_jobs=snapshot_jobs,
    )
    return {
        "status": build.get("status"),
        "normalization": normalization,
        "build": build,
    }

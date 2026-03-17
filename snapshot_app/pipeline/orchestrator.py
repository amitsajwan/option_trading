from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_batch import run_snapshot_batch

from .config import (
    DEFAULT_NORMALIZE_JOBS,
    DEFAULT_PARQUET_BASE,
    DEFAULT_RAW_DATA_ROOT,
    DEFAULT_SNAPSHOT_JOBS,
)
from .normalize import normalize_raw_to_parquet


def _build_year_slices(
    *,
    store: ParquetStore,
    min_day: str | None,
    max_day: str | None,
    explicit_days: Optional[Sequence[str]],
) -> list[dict[str, Any]]:
    if explicit_days:
        grouped: dict[int, list[str]] = {}
        for day in sorted({str(day) for day in explicit_days if str(day).strip()}):
            year = int(pd.Timestamp(day).year)
            grouped.setdefault(year, []).append(day)
        return [
            {
                "year": int(year),
                "min_day": days[0],
                "max_day": days[-1],
                "explicit_days": days,
            }
            for year, days in sorted(grouped.items())
        ]

    days = store.available_days(min_day=min_day, max_day=max_day)
    grouped: dict[int, list[str]] = {}
    for day in days:
        year = int(pd.Timestamp(day).year)
        grouped.setdefault(year, []).append(str(day))
    return [
        {
            "year": int(year),
            "min_day": year_days[0],
            "max_day": year_days[-1],
            "explicit_days": None,
        }
        for year, year_days in sorted(grouped.items())
    ]


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
) -> dict[str, Any]:
    resolved_base = Path(parquet_base)
    store = ParquetStore(resolved_base, snapshots_dataset=output_dataset)
    slices = _build_year_slices(
        store=store,
        min_day=min_day,
        max_day=max_day,
        explicit_days=explicit_days,
    )
    if len(slices) <= 1 or int(snapshot_jobs) <= 1:
        return run_snapshot_batch(
            parquet_base=resolved_base,
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
        )

    payloads = [
        {
            "parquet_base": resolved_base,
            "instrument": instrument,
            "min_day": str(partition["min_day"]),
            "max_day": str(partition["max_day"]),
            "explicit_days": partition["explicit_days"],
            "lookback_days": lookback_days,
            "resume": resume,
            "dry_run": dry_run,
            "log_every": log_every,
            "write_batch_days": write_batch_days,
            "output_dataset": output_dataset,
            "build_source": build_source,
            "build_run_id": build_run_id,
            "validate_ml_flat_contract": validate_ml_flat_contract,
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
                }
                slice_results.append(result)
            except Exception as exc:
                errors.append(
                    {
                        "min_day": str(payload.get("min_day") or ""),
                        "max_day": str(payload.get("max_day") or ""),
                        "error": str(exc),
                    }
                )

    if errors:
        return {
            "status": "partial_error",
            "output_dataset": output_dataset,
            "build_source": build_source,
            "build_run_id": build_run_id,
            "parallel_year_slices": len(payloads),
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
        "parallel_year_slices": len(payloads),
        "days_available": int(sum(int(row.get("days_available") or 0) for row in slice_results)),
        "days_pending": int(sum(int(row.get("days_pending") or 0) for row in slice_results)),
        "days_processed": int(sum(int(row.get("days_processed") or 0) for row in slice_results)),
        "days_skipped_existing": int(sum(int(row.get("days_skipped_existing") or 0) for row in slice_results)),
        "days_skipped_missing_inputs": int(sum(int(row.get("days_skipped_missing_inputs") or 0) for row in slice_results)),
        "days_no_rows": int(sum(int(row.get("days_no_rows") or 0) for row in slice_results)),
        "error_count": int(sum(int(row.get("error_count") or 0) for row in slice_results)),
        "error_days": [
            day
            for row in slice_results
            for day in list(row.get("error_days") or [])
        ],
        "total_rows": int(sum(int(row.get("total_rows") or 0) for row in slice_results)),
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

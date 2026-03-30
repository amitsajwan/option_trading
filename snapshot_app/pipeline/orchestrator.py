from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.derived_batch import run_derived_batch
from snapshot_app.historical.snapshot_batch import (
    CANONICAL_OUTPUT_DATASETS,
    DERIVED_OUTPUT_DATASETS,
    OUTPUT_DATASET_MARKET_BASE,
    OUTPUT_DATASET_ML_FLAT,
    OUTPUT_DATASET_SNAPSHOTS,
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
DEFAULT_BUILD_STAGE = "all"


def _normalize_build_stage(value: str | None) -> str:
    stage = str(value or DEFAULT_BUILD_STAGE).strip().lower() or DEFAULT_BUILD_STAGE
    if stage not in {"all", "snapshots", "derived"}:
        raise ValueError(f"unsupported build_stage: {stage}")
    return stage


def _output_datasets(build_stage: str) -> tuple[str, ...]:
    resolved = _normalize_build_stage(build_stage)
    if resolved == "snapshots":
        return CANONICAL_OUTPUT_DATASETS
    if resolved == "derived":
        return DERIVED_OUTPUT_DATASETS
    return (*CANONICAL_OUTPUT_DATASETS, *DERIVED_OUTPUT_DATASETS)


def _available_target_days(
    *,
    store: ParquetStore,
    min_day: str | None,
    max_day: str | None,
    explicit_days: Optional[Sequence[str]],
    build_stage: str,
) -> list[str]:
    resolved = _normalize_build_stage(build_stage)
    if resolved == "derived":
        days = store.available_snapshot_days(min_day=min_day, max_day=max_day)
    else:
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
    build_stage: str,
) -> set[str]:
    day_sets: list[set[str]] = []
    for dataset_name in _output_datasets(build_stage):
        dataset_store = ParquetStore(parquet_base, snapshots_dataset=dataset_name)
        days = set(dataset_store.available_snapshot_days(min_day=min_day, max_day=max_day))
        day_sets.append(days.intersection(requested_days))
    if not day_sets:
        return set()
    return set.intersection(*day_sets)


def _has_legacy_year_layout(parquet_base: Path, *, build_stage: str) -> bool:
    for dataset_name in _output_datasets(build_stage):
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


def _contract_validation_metadata(*, build_stage: str, validate_ml_flat_contract: bool) -> dict[str, Any]:
    resolved_stage = _normalize_build_stage(build_stage)
    requested = bool(validate_ml_flat_contract)
    if resolved_stage == "snapshots":
        return {
            "contract_validation_requested": requested,
            "contract_validation_enabled": False,
            "contract_validation_scope": "canonical_market_snapshot_only",
            "contract_validation_note": (
                "Canonical MarketSnapshot validation is always enforced during snapshot builds. "
                "validate_ml_flat_contract only applies once derived SnapshotMLFlat rows are built."
            ),
        }
    if resolved_stage == "all":
        return {
            "contract_validation_requested": requested,
            "contract_validation_enabled": requested,
            "contract_validation_scope": "derived_snapshot_ml_flat",
            "contract_validation_note": (
                "For build_stage=all, validate_ml_flat_contract applies to the derived stage. "
                "The canonical snapshots stage still enforces MarketSnapshot validation separately."
            ),
        }
    return {
        "contract_validation_requested": requested,
        "contract_validation_enabled": requested,
        "contract_validation_scope": "derived_snapshot_ml_flat",
    }


def _build_slice_entry(payload: dict[str, Any]) -> dict[str, Any]:
    work = dict(payload)
    stage = _normalize_build_stage(str(work.pop("build_stage", None) or DEFAULT_BUILD_STAGE))
    if stage == "derived":
        return run_derived_batch(**work)
    return run_snapshot_batch(**work)


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
    build_stage: str = DEFAULT_BUILD_STAGE,
) -> dict[str, Any]:
    resolved_build_stage = _normalize_build_stage(build_stage)
    resolved_base = Path(parquet_base)

    if resolved_build_stage == "all":
        snapshots_result = run_snapshot_builds(
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
            output_dataset=OUTPUT_DATASET_SNAPSHOTS,
            build_source=build_source,
            build_run_id=build_run_id,
            validate_ml_flat_contract=False,
            snapshot_jobs=snapshot_jobs,
            slice_months=slice_months,
            slice_warmup_days=slice_warmup_days,
            build_stage="snapshots",
        )
        derived_result = run_snapshot_builds(
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
            output_dataset=OUTPUT_DATASET_ML_FLAT,
            build_source=build_source,
            build_run_id=build_run_id,
            validate_ml_flat_contract=validate_ml_flat_contract,
            snapshot_jobs=snapshot_jobs,
            slice_months=slice_months,
            slice_warmup_days=0,
            build_stage="derived",
        )
        statuses = {str(snapshots_result.get("status") or ""), str(derived_result.get("status") or "")}
        status = "complete"
        if str(snapshots_result.get("status") or "") == "dry_run" and str(derived_result.get("status") or "") in {"dry_run", "no_days"}:
            status = "dry_run"
        elif str(snapshots_result.get("status") or "") == "already_complete" and str(derived_result.get("status") or "") in {"already_complete", "no_days"}:
            status = "already_complete"
        elif "partial_error" in statuses:
            status = "partial_error"
        elif "partial_incomplete" in statuses:
            status = "partial_incomplete"
        elif statuses == {"dry_run"}:
            status = "dry_run"
        elif statuses == {"already_complete"}:
            status = "already_complete"
        elif "no_days" in statuses and len(statuses) == 1:
            status = "no_days"
        return {
            "status": status,
            "build_stage": resolved_build_stage,
            "output_dataset": OUTPUT_DATASET_ML_FLAT,
            "build_source": build_source,
            "build_run_id": build_run_id,
            **_contract_validation_metadata(
                build_stage=resolved_build_stage,
                validate_ml_flat_contract=validate_ml_flat_contract,
            ),
            "days_available": int(max(int(snapshots_result.get("days_available") or 0), int(derived_result.get("days_available") or 0))),
            "days_pending": int(max(int(snapshots_result.get("days_pending") or 0), int(derived_result.get("days_pending") or 0))),
            "days_processed": int(max(int(snapshots_result.get("days_processed") or 0), int(derived_result.get("days_processed") or 0))),
            "warmup_days_processed": int(snapshots_result.get("warmup_days_processed") or 0),
            "days_skipped_existing": int(max(int(snapshots_result.get("days_skipped_existing") or 0), int(derived_result.get("days_skipped_existing") or 0))),
            "days_skipped_missing_inputs": int(int(snapshots_result.get("days_skipped_missing_inputs") or 0) + int(derived_result.get("days_skipped_missing_inputs") or 0)),
            "missing_input_days": [
                *list(snapshots_result.get("missing_input_days") or []),
                *list(derived_result.get("missing_input_days") or []),
            ][:50],
            "days_no_rows": int(int(snapshots_result.get("days_no_rows") or 0) + int(derived_result.get("days_no_rows") or 0)),
            "no_row_days": [
                *list(snapshots_result.get("no_row_days") or []),
                *list(derived_result.get("no_row_days") or []),
            ][:50],
            "error_count": int(int(snapshots_result.get("error_count") or 0) + int(derived_result.get("error_count") or 0)),
            "error_days": [
                *list(snapshots_result.get("error_days") or []),
                *list(derived_result.get("error_days") or []),
            ][:50],
            "total_rows": int(derived_result.get("total_rows") or 0),
            "total_snapshot_rows": int(snapshots_result.get("total_snapshot_rows") or 0),
            "total_market_base_rows": int(snapshots_result.get("total_market_base_rows") or 0),
            "iv_diagnostics": snapshots_result.get("iv_diagnostics") or {},
            "iv_diagnostics_days_with_failures": list(snapshots_result.get("iv_diagnostics_days_with_failures") or []),
            "elapsed_sec": float(snapshots_result.get("elapsed_sec") or 0.0) + float(derived_result.get("elapsed_sec") or 0.0),
            "written_datasets": list((*CANONICAL_OUTPUT_DATASETS, *DERIVED_OUTPUT_DATASETS)),
            "snapshots_stage": snapshots_result,
            "derived_stage": derived_result,
        }

    source_dataset = OUTPUT_DATASET_MARKET_BASE if resolved_build_stage == "derived" else OUTPUT_DATASET_SNAPSHOTS
    store = ParquetStore(resolved_base, snapshots_dataset=source_dataset)
    target_days = _available_target_days(
        store=store,
        min_day=min_day,
        max_day=max_day,
        explicit_days=explicit_days,
        build_stage=resolved_build_stage,
    )
    if resolved_build_stage == "snapshots":
        option_ready_days = set(store.all_days_with_options(min_day=min_day, max_day=max_day))
        target_days = [day for day in target_days if day in option_ready_days]
    if not target_days:
        return {
            "status": "no_days",
            "output_dataset": output_dataset,
            "build_stage": resolved_build_stage,
            **_contract_validation_metadata(
                build_stage=resolved_build_stage,
                validate_ml_flat_contract=validate_ml_flat_contract,
            ),
            "days_available": 0,
        }

    completed_days = (
        _completed_output_days(
            parquet_base=resolved_base,
            min_day=min_day,
            max_day=max_day,
            requested_days=set(target_days),
            build_stage=resolved_build_stage,
        )
        if resume
        else set()
    )
    pending_target_days = [day for day in target_days if day not in completed_days]
    if not pending_target_days:
        return {
            "status": "already_complete",
            "output_dataset": output_dataset,
            "build_stage": resolved_build_stage,
            **_contract_validation_metadata(
                build_stage=resolved_build_stage,
                validate_ml_flat_contract=validate_ml_flat_contract,
            ),
            "days_available": len(target_days),
            "days_skipped_existing": len(completed_days),
            "days_pending": 0,
        }

    batch_fn = run_snapshot_batch if resolved_build_stage == "snapshots" else run_derived_batch
    if int(snapshot_jobs) <= 1:
        return batch_fn(
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

    if _has_legacy_year_layout(resolved_base, build_stage=resolved_build_stage):
        raise RuntimeError(
            "parallel chunk snapshot builds require a clean snapshot output root. "
            "Found legacy yearly parquet files under the selected output datasets. "
            "Delete those datasets and rerun the build."
        )

    history_max_day = max(pending_target_days)
    if resolved_build_stage == "derived":
        history_days = store.available_snapshot_days(min_day=None, max_day=history_max_day)
        effective_warmup_days = 0
    else:
        history_days = store.available_days(min_day=None, max_day=history_max_day)
        effective_warmup_days = max(0, int(slice_warmup_days))
    slices = _build_parallel_slices(
        history_days=history_days,
        target_days=pending_target_days,
        slice_months=max(1, int(slice_months)),
        warmup_days=effective_warmup_days,
    )
    if len(slices) <= 1:
        only = slices[0]
        return batch_fn(
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
            "build_stage": resolved_build_stage,
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
                result["build_stage"] = resolved_build_stage
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
            "build_stage": resolved_build_stage,
            "build_source": build_source,
            "build_run_id": build_run_id,
            **_contract_validation_metadata(
                build_stage=resolved_build_stage,
                validate_ml_flat_contract=validate_ml_flat_contract,
            ),
            "parallel_slices": len(payloads),
            "parallel_slice_months": int(slice_months),
            "parallel_slice_warmup_days": int(effective_warmup_days),
            "slice_results": slice_results,
            "errors": errors,
        }

    statuses = {str(row.get("status") or "") for row in slice_results}
    status = "complete"
    if "partial_error" in statuses:
        status = "partial_error"
    elif "partial_incomplete" in statuses:
        status = "partial_incomplete"
    elif statuses == {"dry_run"}:
        status = "dry_run"
    elif statuses == {"already_complete"}:
        status = "already_complete"
    elif "no_days" in statuses and len(statuses) == 1:
        status = "no_days"

    return {
        "status": status,
        "output_dataset": output_dataset,
        "build_stage": resolved_build_stage,
        "build_source": build_source,
        "build_run_id": build_run_id,
        **_contract_validation_metadata(
            build_stage=resolved_build_stage,
            validate_ml_flat_contract=validate_ml_flat_contract,
        ),
        "parallel_slices": len(payloads),
        "parallel_slice_months": int(slice_months),
        "parallel_slice_warmup_days": int(effective_warmup_days),
        "days_available": int(len(target_days)),
        "days_pending": int(len(pending_target_days)),
        "days_processed": int(sum(int(row.get("days_processed") or 0) for row in slice_results)),
        "warmup_days_processed": int(sum(int(row.get("warmup_days_processed") or 0) for row in slice_results)),
        "days_skipped_existing": int(len(completed_days)),
        "days_skipped_missing_inputs": int(sum(int(row.get("days_skipped_missing_inputs") or 0) for row in slice_results)),
        "missing_input_days": [
            day
            for row in slice_results
            for day in list(row.get("missing_input_days") or [])
        ],
        "days_no_rows": int(sum(int(row.get("days_no_rows") or 0) for row in slice_results)),
        "no_row_days": [
            day
            for row in slice_results
            for day in list(row.get("no_row_days") or [])
        ],
        "error_count": int(sum(int(row.get("error_count") or 0) for row in slice_results)),
        "error_days": [
            day
            for row in slice_results
            for day in list(row.get("error_days") or [])
        ],
        "total_rows": int(sum(int(row.get("total_rows") or 0) for row in slice_results)),
        "total_snapshot_rows": int(sum(int(row.get("total_snapshot_rows") or 0) for row in slice_results)),
        "total_market_base_rows": int(sum(int(row.get("total_market_base_rows") or 0) for row in slice_results)),
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
    build_stage: str = DEFAULT_BUILD_STAGE,
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
        build_stage=build_stage,
    )
    return {
        "status": build.get("status"),
        "normalization": normalization,
        "build": build,
    }

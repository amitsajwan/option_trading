"""Projection stage for derived historical datasets from market_base."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

from snapshot_app.core.stage_views import project_stage_views_from_flat_row

from .parquet_store import ParquetStore
from .snapshot_batch import (
    DERIVED_OUTPUT_DATASETS,
    OUTPUT_DATASET_MARKET_BASE,
    OUTPUT_DATASET_ML_FLAT,
    STAGE_OUTPUT_DATASETS,
    _completed_output_days,
    _default_build_run_id,
    _project_rows_to_ml_flat,
    _validate_ml_flat_rows_or_raise,
    write_days_to_parquet,
)


def _derived_contract_validation_metadata(validate_ml_flat_contract: bool) -> dict[str, Any]:
    enabled = bool(validate_ml_flat_contract)
    return {
        "contract_validation_requested": enabled,
        "contract_validation_enabled": enabled,
        "contract_validation_scope": "derived_snapshot_ml_flat",
    }


def _project_stage_rows_from_market_base(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    projected: dict[str, list[dict[str, Any]]] = {name: [] for name in STAGE_OUTPUT_DATASETS}
    for row in rows:
        trade_date = str(row.get("trade_date") or "").strip()
        year_value = pd.to_numeric(row.get("year"), errors="coerce")
        year = int(year_value) if pd.notna(year_value) else int(pd.Timestamp(trade_date).year)
        build_source = str(row.get("build_source") or "historical")
        build_run_id = str(row.get("build_run_id") or _default_build_run_id())
        for dataset_name, payload in project_stage_views_from_flat_row(row).items():
            stage_row = dict(payload)
            stage_row["trade_date"] = trade_date
            stage_row["year"] = year
            stage_row["build_source"] = build_source
            stage_row["build_run_id"] = build_run_id
            projected[dataset_name].append(stage_row)
    return projected


def run_derived_batch(
    *,
    parquet_base: str | Path,
    min_day: str | None = None,
    max_day: str | None = None,
    explicit_days: list[str] | None = None,
    planned_days: list[str] | None = None,
    emit_days: list[str] | None = None,
    resume: bool = True,
    dry_run: bool = False,
    log_every: int = 10,
    write_batch_days: int = 20,
    build_source: str = "historical",
    build_run_id: str | None = None,
    validate_ml_flat_contract: bool = False,
    partition_key: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Build derived ML-flat and stage parquet from market_base rows."""
    started_at = time.time()
    resolved_build_run_id = str(build_run_id or _default_build_run_id())
    store = ParquetStore(parquet_base, snapshots_dataset=OUTPUT_DATASET_MARKET_BASE)
    out_base = Path(parquet_base)

    requested_days = [str(day) for day in (emit_days or explicit_days or []) if str(day).strip()]
    requested_day_set = set(requested_days)
    planned_day_values = [str(day) for day in (planned_days or []) if str(day).strip()]
    min_bound = min_day
    max_bound = max_day
    if planned_day_values:
        min_bound = min(planned_day_values)
        max_bound = max(planned_day_values)
    elif requested_days:
        min_bound = min_bound or min(requested_days)
        max_bound = max_bound or max(requested_days)

    available_source_days = store.available_snapshot_days(min_day=min_bound, max_day=max_bound)
    available_source_set = set(available_source_days)
    execution_days = list(available_source_days)
    if planned_day_values:
        planned_day_set = set(planned_day_values)
        execution_days = [day for day in execution_days if day in planned_day_set]

    output_days = list(execution_days)
    if requested_day_set:
        output_days = [day for day in execution_days if day in requested_day_set]

    missing_input_days = [day for day in requested_days if day not in available_source_set]
    if not output_days:
        return {
            "status": ("partial_incomplete" if missing_input_days else "no_days"),
            "output_dataset": OUTPUT_DATASET_ML_FLAT,
            "source_dataset": OUTPUT_DATASET_MARKET_BASE,
            **_derived_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": 0,
            "days_skipped_missing_inputs": len(missing_input_days),
            "missing_input_days": missing_input_days[:50],
        }

    already_done = (
        _completed_output_days(
            parquet_base=parquet_base,
            min_day=min_bound,
            max_day=max_bound,
            requested_days=set(output_days),
            dataset_names=DERIVED_OUTPUT_DATASETS,
        )
        if resume
        else set()
    )
    pending_output_days = [day for day in output_days if day not in already_done]

    print(f"[derived_batch] Days available         : {len(output_days)}")
    print(f"[derived_batch] Days already built     : {len(already_done)}")
    print(f"[derived_batch] Days pending           : {len(pending_output_days)}")
    print("[derived_batch] Output datasets        : " + ", ".join(DERIVED_OUTPUT_DATASETS))
    print(f"[derived_batch] Source dataset         : {OUTPUT_DATASET_MARKET_BASE}")
    print(f"[derived_batch] Build source/run       : {build_source}/{resolved_build_run_id}")
    if min_day or max_day:
        print(f"[derived_batch] Date filter            : {min_day or 'start'} -> {max_day or 'end'}")
    if requested_day_set:
        print(f"[derived_batch] Explicit day mode      : {len(output_days)} requested")
    if partition_key:
        print(f"[derived_batch] Output partition       : {partition_key}")

    if dry_run:
        return {
            "status": "dry_run",
            "output_dataset": OUTPUT_DATASET_ML_FLAT,
            "source_dataset": OUTPUT_DATASET_MARKET_BASE,
            **_derived_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": len(output_days),
            "days_pending": len(pending_output_days),
            "days_ready": len(pending_output_days),
            "days_skipped_existing": len(already_done),
            "days_skipped_missing_inputs": len(missing_input_days),
            "missing_input_days": missing_input_days[:50],
            "first_ready_day": pending_output_days[0] if pending_output_days else None,
            "last_ready_day": pending_output_days[-1] if pending_output_days else None,
        }

    if not pending_output_days:
        return {
            "status": ("partial_incomplete" if missing_input_days else "already_complete"),
            "output_dataset": OUTPUT_DATASET_ML_FLAT,
            "source_dataset": OUTPUT_DATASET_MARKET_BASE,
            **_derived_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": len(output_days),
            "days_skipped_existing": len(already_done),
            "days_pending": 0,
            "days_skipped_missing_inputs": len(missing_input_days),
            "missing_input_days": missing_input_days[:50],
        }

    source_frame = store.snapshots_for_date_range(min_bound or pending_output_days[0], max_bound or pending_output_days[-1])
    if len(source_frame) == 0:
        return {
            "status": "partial_incomplete",
            "output_dataset": OUTPUT_DATASET_ML_FLAT,
            "source_dataset": OUTPUT_DATASET_MARKET_BASE,
            **_derived_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": len(output_days),
            "days_pending": len(pending_output_days),
            "days_skipped_missing_inputs": len(missing_input_days) + len(pending_output_days),
            "missing_input_days": (missing_input_days + pending_output_days)[:50],
        }

    source_frame["trade_date"] = source_frame["trade_date"].astype(str)
    grouped = {
        str(day): frame.sort_values("timestamp").reset_index(drop=True)
        for day, frame in source_frame.groupby("trade_date", sort=False)
    }

    days_done = 0
    no_rows_days: list[str] = []
    error_days: list[str] = []
    total_rows = 0
    batch_days = max(1, int(write_batch_days))
    buffered_year: int | None = None
    buffered_ml_flat_rows: list[dict[str, Any]] = []
    buffered_stage_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in STAGE_OUTPUT_DATASETS}
    buffered_trade_dates: set[str] = set()
    buffered_day_count = 0

    def _flush_buffer() -> None:
        nonlocal total_rows, buffered_ml_flat_rows, buffered_stage_rows, buffered_trade_dates, buffered_day_count
        if buffered_year is None or (not buffered_ml_flat_rows and not any(buffered_stage_rows.values())):
            return
        total_rows += write_days_to_parquet(
            buffered_ml_flat_rows,
            out_base=out_base,
            year=buffered_year,
            output_dataset=OUTPUT_DATASET_ML_FLAT,
            replace_trade_dates=buffered_trade_dates,
            partition_key=partition_key,
        )
        for dataset_name in STAGE_OUTPUT_DATASETS:
            write_days_to_parquet(
                buffered_stage_rows.get(dataset_name, []),
                out_base=out_base,
                year=buffered_year,
                output_dataset=dataset_name,
                replace_trade_dates=buffered_trade_dates,
                partition_key=partition_key,
            )
        buffered_ml_flat_rows = []
        buffered_stage_rows = {name: [] for name in STAGE_OUTPUT_DATASETS}
        buffered_trade_dates = set()
        buffered_day_count = 0

    for idx, day in enumerate(pending_output_days):
        if idx == 0 or (idx % max(1, int(log_every)) == 0):
            print(f"[derived_batch] Processing {idx + 1}/{len(pending_output_days)} day={day}", flush=True)
        try:
            day_frame = grouped.get(str(day))
            if day_frame is None or len(day_frame) == 0:
                no_rows_days.append(str(day))
                continue
            day_rows = day_frame.to_dict("records")
            ml_flat_rows = _project_rows_to_ml_flat(day_rows)
            if validate_ml_flat_contract:
                _validate_ml_flat_rows_or_raise(ml_flat_rows)
            stage_rows = _project_stage_rows_from_market_base(day_rows)
            if not ml_flat_rows:
                no_rows_days.append(str(day))
                continue

            year = int(pd.Timestamp(day).year)
            if buffered_year is None:
                buffered_year = year
            if year != buffered_year:
                _flush_buffer()
                buffered_year = year

            buffered_ml_flat_rows.extend(ml_flat_rows)
            for dataset_name in STAGE_OUTPUT_DATASETS:
                buffered_stage_rows[dataset_name].extend(stage_rows.get(dataset_name, []))
            buffered_trade_dates.add(str(day))
            buffered_day_count += 1
            if buffered_day_count >= batch_days:
                _flush_buffer()
            days_done += 1
        except Exception:
            error_days.append(str(day))
            raise

    _flush_buffer()

    final_status = "complete"
    if missing_input_days or no_rows_days:
        final_status = "partial_incomplete"
    if error_days:
        final_status = "partial_error"
    elapsed = round(time.time() - started_at, 2)
    return {
        "status": final_status,
        "output_dataset": OUTPUT_DATASET_ML_FLAT,
        "source_dataset": OUTPUT_DATASET_MARKET_BASE,
        "build_source": build_source,
        "build_run_id": resolved_build_run_id,
        "partition_key": str(partition_key or ""),
        **_derived_contract_validation_metadata(validate_ml_flat_contract),
        "days_available": len(output_days),
        "days_pending": len(pending_output_days),
        "days_processed": days_done,
        "warmup_days_processed": 0,
        "days_skipped_existing": len(already_done),
        "days_skipped_missing_inputs": len(missing_input_days),
        "missing_input_days": missing_input_days[:50],
        "days_no_rows": len(no_rows_days),
        "no_row_days": no_rows_days[:50],
        "error_count": len(error_days),
        "error_days": error_days[:50],
        "total_rows": int(total_rows),
        "total_snapshot_rows": 0,
        "total_market_base_rows": 0,
        "written_datasets": list(DERIVED_OUTPUT_DATASETS),
        "iv_diagnostics": {},
        "iv_diagnostics_days_with_failures": [],
        "elapsed_sec": elapsed,
    }

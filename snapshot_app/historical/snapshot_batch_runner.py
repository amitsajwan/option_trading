"""CLI for historical Layer-2 snapshot batch build and validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from snapshot_app.snapshot_ml_flat_contract import validate_snapshot_ml_flat_frame

from .parquet_store import ParquetStore
from .snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE
from .snapshot_batch import (
    ML_FLAT_SCHEMA_VERSION,
    OUTPUT_DATASET_ML_FLAT,
    run_snapshot_batch,
)

DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_INSTRUMENT = "BANKNIFTY-I"
DEFAULT_OUTPUT_DATASET = OUTPUT_DATASET_ML_FLAT
DEFAULT_REQUIRED_FIELDS_ML_FLAT = ["opt_flow_rows"]
DEFAULT_REQUIRED_ML_FLAT_VERSION = ML_FLAT_SCHEMA_VERSION
DEFAULT_WINDOW_MIN_TRADING_DAYS = 150
DEFAULT_WINDOW_MAX_GAP_DAYS = 7


def _year_date_bounds(year: int) -> tuple[str, str]:
    y = int(year)
    return (f"{y:04d}-01-01", f"{y:04d}-12-31")


def _coerce_day_or_none(value: str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts)


def _clip_year_range(
    year: int,
    *,
    min_day: str | None,
    max_day: str | None,
) -> tuple[str, str]:
    year_start, year_end = _year_date_bounds(year)
    start_ts = _coerce_day_or_none(min_day) or pd.Timestamp(year_start)
    end_ts = _coerce_day_or_none(max_day) or pd.Timestamp(year_end)
    start = max(pd.Timestamp(year_start), start_ts)
    end = min(pd.Timestamp(year_end), end_ts)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


def _build_year_slices(
    store: ParquetStore,
    *,
    min_day: str | None,
    max_day: str | None,
) -> list[dict[str, Any]]:
    days = store.available_days(min_day=min_day, max_day=max_day)
    if not days:
        return []

    grouped: dict[int, list[str]] = {}
    for day in days:
        year = int(pd.Timestamp(day).year)
        grouped.setdefault(year, []).append(str(day))

    out: list[dict[str, Any]] = []
    for year in sorted(grouped):
        year_days = grouped[year]
        out.append(
            {
                "year": int(year),
                "min_day": year_days[0],
                "max_day": year_days[-1],
                "trading_days": int(len(year_days)),
            }
        )
    return out


def _artifact_path_for_year(path_value: str | None, year: int) -> str | None:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return str(path.parent / f"year={int(year)}" / path.name)


def _quote_cli_value(value: str | Path) -> str:
    text = str(value)
    escaped = text.replace('"', '\\"')
    return f'"{escaped}"'


def _build_parallel_year_command(
    *,
    base: Path,
    instrument: str,
    year_slice: dict[str, Any],
    lookback_days: int,
    no_resume: bool,
    validate_days: int,
    build_source: str,
    validate_ml_flat_contract: bool,
    manifest_out: str | None,
    validation_report_out: str | None,
    required_schema_version: str,
) -> str:
    year = int(year_slice["year"])
    parts = [
        "python -m snapshot_app.historical.snapshot_batch_runner",
        f"--base {_quote_cli_value(base)}",
        f"--year {year}",
        f"--lookback-days {int(lookback_days)}",
        f"--instrument {_quote_cli_value(instrument)}",
        f"--build-source {_quote_cli_value(build_source)}",
        f"--required-schema-version {_quote_cli_value(required_schema_version)}",
    ]
    if no_resume:
        parts.append("--no-resume")
    if validate_ml_flat_contract:
        parts.append("--validate-ml-flat-contract")
    if validate_days > 0:
        parts.append(f"--validate-days {int(validate_days)}")
    manifest_path = _artifact_path_for_year(manifest_out, year)
    if manifest_path:
        parts.append(f"--manifest-out {_quote_cli_value(manifest_path)}")
    validation_path = _artifact_path_for_year(validation_report_out, year)
    if validation_path:
        parts.append(f"--validation-report-out {_quote_cli_value(validation_path)}")
    return " ".join(parts)


def _extract_live_fields(payload: dict[str, Any]) -> set[str]:
    """Extract flattened field names from live snapshot payload/envelope."""
    if not isinstance(payload, dict):
        return set()

    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
    if not isinstance(snapshot, dict):
        return set()

    fields: set[str] = set()
    for key in ("snapshot_id", "instrument", "schema_name"):
        if key in snapshot:
            fields.add(key)
    if ("version" in snapshot) or ("schema_version" in snapshot):
        fields.add("schema_version")

    flatten_blocks = (
        "session_context",
        "futures_bar",
        "futures_derived",
        "mtf_derived",
        "opening_range",
        "vix_context",
        "chain_aggregates",
        "atm_options",
        "iv_derived",
        "option_price",
        "session_levels",
    )
    for block_name in flatten_blocks:
        block = snapshot.get(block_name)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, (dict, list, tuple, set)):
                continue
            fields.add(str(key))
    return fields


def _print_iv_diagnostics_summary(result: dict[str, Any]) -> None:
    iv = result.get("iv_diagnostics")
    if not isinstance(iv, dict):
        return

    minutes = int(iv.get("minutes") or 0)
    print("\n[iv] Summary")
    print(
        "  minutes={minutes} ce_non_null={ce_non_null} pe_non_null={pe_non_null} "
        "ce_from_feed={ce_feed} pe_from_feed={pe_feed} "
        "ce_from_solver={ce_solver} pe_from_solver={pe_solver} "
        "ce_solver_failed={ce_fail} pe_solver_failed={pe_fail}".format(
            minutes=minutes,
            ce_non_null=int(iv.get("ce_iv_non_null") or 0),
            pe_non_null=int(iv.get("pe_iv_non_null") or 0),
            ce_feed=int(iv.get("ce_iv_from_feed") or 0),
            pe_feed=int(iv.get("pe_iv_from_feed") or 0),
            ce_solver=int(iv.get("ce_iv_from_solver") or 0),
            pe_solver=int(iv.get("pe_iv_from_solver") or 0),
            ce_fail=int(iv.get("ce_iv_solver_failed") or 0),
            pe_fail=int(iv.get("pe_iv_solver_failed") or 0),
        )
    )

    failure_days = result.get("iv_diagnostics_days_with_failures")
    if not isinstance(failure_days, list) or not failure_days:
        print("  failure_days=0")
        return

    print("\n[iv] Failure Days")
    print(
        "  {day:<10} {mins:>6} {ce_ok:>6} {pe_ok:>6} {ce_sf:>7} {pe_sf:>7} {ce_um:>7} {pe_um:>7}".format(
            day="trade_date",
            mins="mins",
            ce_ok="ce_ok",
            pe_ok="pe_ok",
            ce_sf="ce_fail",
            pe_sf="pe_fail",
            ce_um="ce_unexp",
            pe_um="pe_unexp",
        )
    )
    for row in failure_days:
        if not isinstance(row, dict):
            continue
        print(
            "  {day:<10} {mins:>6} {ce_ok:>6} {pe_ok:>6} {ce_sf:>7} {pe_sf:>7} {ce_um:>7} {pe_um:>7}".format(
                day=str(row.get("trade_date") or "")[:10],
                mins=int(row.get("minutes") or 0),
                ce_ok=int(row.get("ce_iv_non_null") or 0),
                pe_ok=int(row.get("pe_iv_non_null") or 0),
                ce_sf=int(row.get("ce_iv_solver_failed") or 0),
                pe_sf=int(row.get("pe_iv_solver_failed") or 0),
                ce_um=int(row.get("ce_iv_unexpected_missing") or 0),
                pe_um=int(row.get("pe_iv_unexpected_missing") or 0),
            )
        )


def _validate_ml_flat_frame(frame: pd.DataFrame) -> dict[str, Any]:
    return validate_snapshot_ml_flat_frame(frame, raise_on_error=False)


def validate_output(
    parquet_base: Path,
    n_days: int = 5,
    live_snapshot_path: str | None = None,
    *,
    output_dataset: str = DEFAULT_OUTPUT_DATASET,
    required_schema_version: str = DEFAULT_REQUIRED_ML_FLAT_VERSION,
    min_day: str | None = None,
    max_day: str | None = None,
) -> dict[str, Any]:
    """Validate produced snapshots for row counts, null rates, and schema parity."""
    store = ParquetStore(parquet_base, snapshots_dataset=output_dataset)
    snapshot_days = store.available_snapshot_days(min_day=min_day, max_day=max_day)
    if not snapshot_days:
        print(f"[validate] No snapshot days found in dataset={output_dataset}. Build snapshots first.")
        return {
            "ok": False,
            "output_dataset": output_dataset,
            "min_day": min_day,
            "max_day": max_day,
            "reason": "no_snapshot_days",
        }

    sample_days = snapshot_days[-max(1, int(n_days)) :]
    print(f"[validate] dataset={output_dataset} checking {len(sample_days)} recent day(s): {sample_days}")

    print("\n[validate] Row counts per day")
    all_ok = True
    row_counts: list[dict[str, Any]] = []
    for day in sample_days:
        df = store.snapshots_for_date_range(day, day)
        rows = int(len(df))
        status = "OK" if 370 <= rows <= 380 else ("WARN" if 300 <= rows <= 400 else "ERROR")
        if status != "OK":
            all_ok = False
        print(f"  {day}: {rows} rows [{status}]")
        row_counts.append({"trade_date": str(day), "rows": rows, "status": status})
    if all_ok:
        print("  All row counts within expected range (370-380)")

    df_range = store.snapshots_for_date_range(sample_days[0], sample_days[-1])
    if len(df_range) == 0:
        print("[validate] No rows in selected date range.")
        return {
            "ok": False,
            "output_dataset": output_dataset,
            "sample_days": sample_days,
            "row_counts": row_counts,
            "reason": "no_rows_in_range",
        }

    print("\n[validate] Team A contract validation (SnapshotMLFlatV1)")
    report = _validate_ml_flat_frame(df_range)
    print(json.dumps(report, indent=2))
    return {
        "ok": bool(report.get("ok")) and all_ok,
        "output_dataset": output_dataset,
        "min_day": min_day,
        "max_day": max_day,
        "sample_days": sample_days,
        "row_counts": row_counts,
        "required_schema_version": str(required_schema_version),
        "contract_report": report,
    }


def find_days_missing_fields(
    store: ParquetStore,
    *,
    fields: list[str],
    min_day: str | None = None,
    max_day: str | None = None,
) -> list[str]:
    """Return snapshot days where any required field is entirely absent/null."""
    coverage = store.snapshot_field_coverage(fields=fields, min_day=min_day, max_day=max_day)
    if len(coverage) == 0:
        return []

    missing_days: list[str] = []
    for _, row in coverage.iterrows():
        for field in fields:
            key = f"nn__{field}"
            if int(row.get(key) or 0) <= 0:
                missing_days.append(str(row["trade_date"]))
                break
    return missing_days


def find_days_mismatched_schema_version(
    store: ParquetStore,
    *,
    required_schema_version: str = DEFAULT_REQUIRED_ML_FLAT_VERSION,
    min_day: str | None = None,
    max_day: str | None = None,
) -> list[str]:
    """Return snapshot days where schema_version is not fully on required version."""
    coverage = store.snapshot_schema_version_coverage(min_day=min_day, max_day=max_day)
    if len(coverage) == 0:
        return []

    out: list[str] = []
    for _, row in coverage.iterrows():
        row_count = int(row.get("row_count") or 0)
        row_min = str(row.get("min_schema_version") or "").strip()
        row_max = str(row.get("max_schema_version") or "").strip()
        if row_count <= 0:
            continue
        if row_min == str(required_schema_version) and row_max == str(required_schema_version):
            continue
        out.append(str(row["trade_date"]))
    return out


def build_window_readiness_artifact(
    store: ParquetStore,
    *,
    min_day: str | None,
    max_day: str | None,
    required_schema_version: str = DEFAULT_REQUIRED_ML_FLAT_VERSION,
    min_trading_days: int = DEFAULT_WINDOW_MIN_TRADING_DAYS,
    max_gap_days: int = DEFAULT_WINDOW_MAX_GAP_DAYS,
) -> dict[str, Any]:
    return store.build_window_readiness_artifact(
        required_schema_version=required_schema_version,
        min_day=min_day,
        max_day=max_day,
        min_trading_days=min_trading_days,
        max_gap_days=max_gap_days,
    )


def write_window_readiness_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")


def write_json_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build historical Layer-2 snapshots from parquet Layer-1 inputs.")
    parser.add_argument("--base", default=str(DEFAULT_PARQUET_BASE), help=f"Parquet base path (default: {DEFAULT_PARQUET_BASE})")
    parser.add_argument("--instrument", default=DEFAULT_INSTRUMENT, help=f"Snapshot instrument (default: {DEFAULT_INSTRUMENT})")
    parser.add_argument("--year", type=int, default=None, help="Restrict build/validation window to one calendar year.")
    parser.add_argument(
        "--plan-year-runs",
        action="store_true",
        help="Print calendar-year slices and ready-to-run per-year commands, then exit.",
    )
    parser.add_argument("--min-day", default=None, help="Start date inclusive (YYYY-MM-DD)")
    parser.add_argument("--max-day", default=None, help="End date inclusive (YYYY-MM-DD)")
    parser.add_argument("--lookback-days", type=int, default=30, help="Futures lookback trading days for rolling metrics")
    parser.add_argument("--no-resume", action="store_true", help="Rebuild days even if already in snapshots parquet")
    parser.add_argument("--dry-run", action="store_true", help="Plan only, do not write")
    parser.add_argument("--validate-only", action="store_true", help="Skip build and run validation only")
    parser.add_argument("--validate-days", type=int, default=0, help="Validate recent N snapshot days after build (or in validate-only mode)")
    parser.add_argument("--live-snapshot", default=None, help="Path to live snapshot JSONL for schema comparison")
    parser.add_argument("--log-every", type=int, default=10, help="Print progress every N days")
    parser.add_argument(
        "--write-batch-days",
        type=int,
        default=20,
        help="Flush yearly snapshot parquet every N processed days (default: 20).",
    )
    parser.add_argument(
        "--required-fields",
        nargs="+",
        default=None,
        help=(
            "Fields that must be populated in snapshots_ml_flat "
            f"(default: {' '.join(DEFAULT_REQUIRED_FIELDS_ML_FLAT)})."
        ),
    )
    parser.add_argument(
        "--required-schema-version",
        default=None,
        help=(
            "Override required schema version for rebuild checks/validation "
            f"(default: {DEFAULT_REQUIRED_ML_FLAT_VERSION})."
        ),
    )
    parser.add_argument(
        "--rebuild-missing-fields",
        action="store_true",
        help="Detect days missing required fields or not on required schema version and rebuild only those days.",
    )
    parser.add_argument(
        "--print-iv-diagnostics",
        action="store_true",
        help="Print compact IV diagnostics summary from batch output.",
    )
    parser.add_argument(
        "--build-source",
        default="historical",
        help="Build source metadata for SnapshotMLFlat rows (default: historical).",
    )
    parser.add_argument(
        "--build-run-id",
        default=None,
        help="Optional build run id for SnapshotMLFlat rows (default: auto UTC stamp).",
    )
    parser.add_argument(
        "--validate-ml-flat-contract",
        action="store_true",
        help="Validate each processed day against Team A SnapshotMLFlat contract during build.",
    )
    parser.add_argument(
        "--manifest-out",
        default=None,
        help="Optional path to write Team B build manifest JSON.",
    )
    parser.add_argument(
        "--validation-report-out",
        default=None,
        help="Optional path to write Team B validation report JSON.",
    )
    parser.add_argument(
        "--window-manifest-out",
        default=None,
        help="Optional path to write latest contiguous schema window readiness JSON artifact.",
    )
    parser.add_argument(
        "--window-min-trading-days",
        type=int,
        default=DEFAULT_WINDOW_MIN_TRADING_DAYS,
        help=f"Formal readiness minimum trading days (default: {DEFAULT_WINDOW_MIN_TRADING_DAYS}).",
    )
    parser.add_argument(
        "--window-max-gap-days",
        type=int,
        default=DEFAULT_WINDOW_MAX_GAP_DAYS,
        help=f"Maximum calendar gap between contiguous snapshot days (default: {DEFAULT_WINDOW_MAX_GAP_DAYS}).",
    )
    args = parser.parse_args()

    base = Path(args.base)
    if not base.exists():
        print(f"ERROR: parquet base path not found: {base}")
        return 1

    output_dataset = DEFAULT_OUTPUT_DATASET
    effective_min_day = args.min_day
    effective_max_day = args.max_day
    if args.year is not None:
        effective_min_day, effective_max_day = _clip_year_range(
            int(args.year),
            min_day=args.min_day,
            max_day=args.max_day,
        )
    required_schema_version = (
        str(args.required_schema_version).strip()
        if args.required_schema_version
        else DEFAULT_REQUIRED_ML_FLAT_VERSION
    )
    default_required_fields = DEFAULT_REQUIRED_FIELDS_ML_FLAT

    if args.plan_year_runs:
        store = ParquetStore(base, snapshots_dataset=output_dataset)
        year_slices = _build_year_slices(
            store,
            min_day=effective_min_day,
            max_day=effective_max_day,
        )
        commands = [
            {
                "year": int(year_slice["year"]),
                "min_day": str(year_slice["min_day"]),
                "max_day": str(year_slice["max_day"]),
                "trading_days": int(year_slice["trading_days"]),
                "command": _build_parallel_year_command(
                    base=base,
                    instrument=args.instrument,
                    year_slice=year_slice,
                    lookback_days=args.lookback_days,
                    no_resume=bool(args.no_resume),
                    validate_days=int(args.validate_days or 0),
                    build_source=args.build_source,
                    validate_ml_flat_contract=bool(args.validate_ml_flat_contract),
                    manifest_out=args.manifest_out,
                    validation_report_out=args.validation_report_out,
                    required_schema_version=required_schema_version,
                ),
            }
            for year_slice in year_slices
        ]
        payload = {
            "output_dataset": output_dataset,
            "required_schema_version": required_schema_version,
            "min_day": effective_min_day,
            "max_day": effective_max_day,
            "calendar_year_slices": commands,
        }
        print(json.dumps(payload, indent=2))
        return 0

    if args.validate_only:
        validation_report = validate_output(
            base,
            n_days=(args.validate_days or 5),
            live_snapshot_path=args.live_snapshot,
            output_dataset=output_dataset,
            required_schema_version=required_schema_version,
            min_day=effective_min_day,
            max_day=effective_max_day,
        )
        if args.validation_report_out:
            out_path = Path(args.validation_report_out)
            write_json_artifact(out_path, validation_report)
            print(json.dumps({"validation_report_out": str(out_path)}, indent=2))
        if args.window_manifest_out:
            store = ParquetStore(base, snapshots_dataset=output_dataset)
            artifact = build_window_readiness_artifact(
                store,
                min_day=effective_min_day,
                max_day=effective_max_day,
                required_schema_version=required_schema_version,
                min_trading_days=int(args.window_min_trading_days),
                max_gap_days=int(args.window_max_gap_days),
            )
            out_path = Path(args.window_manifest_out)
            write_window_readiness_artifact(out_path, artifact)
            print(json.dumps({"window_manifest_out": str(out_path), "window_readiness": artifact}, indent=2))
        return 0

    explicit_days: list[str] | None = None
    if args.rebuild_missing_fields:
        store = ParquetStore(base, snapshots_dataset=output_dataset)
        required_fields = list(args.required_fields or default_required_fields)
        missing_field_days = find_days_missing_fields(
            store,
            fields=required_fields,
            min_day=effective_min_day,
            max_day=effective_max_day,
        )
        version_mismatch_days = find_days_mismatched_schema_version(
            store,
            required_schema_version=required_schema_version,
            min_day=effective_min_day,
            max_day=effective_max_day,
        )
        explicit_days = sorted(set(missing_field_days) | set(version_mismatch_days))
        print(
            json.dumps(
                {
                    "required_fields": required_fields,
                    "required_version": required_schema_version,
                    "missing_field_days": len(missing_field_days),
                    "version_mismatch_days": len(version_mismatch_days),
                    "days_to_rebuild": len(explicit_days),
                    "first_missing_day": (explicit_days[0] if explicit_days else None),
                    "last_missing_day": (explicit_days[-1] if explicit_days else None),
                },
                indent=2,
            )
        )
        if not explicit_days:
            print("[snapshot_batch_runner] No snapshot days require field backfill.")
            return 0

    result = run_snapshot_batch(
        parquet_base=base,
        instrument=args.instrument,
        min_day=effective_min_day,
        max_day=effective_max_day,
        explicit_days=explicit_days,
        lookback_days=args.lookback_days,
        resume=(False if explicit_days is not None else (not args.no_resume)),
        dry_run=args.dry_run,
        log_every=args.log_every,
        write_batch_days=args.write_batch_days,
        output_dataset=output_dataset,
        build_source=args.build_source,
        build_run_id=args.build_run_id,
        validate_ml_flat_contract=args.validate_ml_flat_contract,
    )
    print(json.dumps(result, indent=2, default=str))
    if args.print_iv_diagnostics:
        _print_iv_diagnostics_summary(result)
    if args.manifest_out:
        manifest_payload = {
            "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
            "output_dataset": output_dataset,
            "required_schema_version": required_schema_version,
            "result": result,
        }
        out_path = Path(args.manifest_out)
        write_json_artifact(out_path, manifest_payload)
        print(json.dumps({"manifest_out": str(out_path)}, indent=2))

    if args.validate_days > 0 and (not args.dry_run):
        validation_report = validate_output(
            base,
            n_days=args.validate_days,
            live_snapshot_path=args.live_snapshot,
            output_dataset=output_dataset,
            required_schema_version=required_schema_version,
            min_day=effective_min_day,
            max_day=effective_max_day,
        )
        if args.validation_report_out:
            out_path = Path(args.validation_report_out)
            write_json_artifact(out_path, validation_report)
            print(json.dumps({"validation_report_out": str(out_path)}, indent=2))
    elif args.validation_report_out and (not args.dry_run):
        validation_report = validate_output(
            base,
            n_days=5,
            live_snapshot_path=args.live_snapshot,
            output_dataset=output_dataset,
            required_schema_version=required_schema_version,
            min_day=effective_min_day,
            max_day=effective_max_day,
        )
        out_path = Path(args.validation_report_out)
        write_json_artifact(out_path, validation_report)
        print(json.dumps({"validation_report_out": str(out_path)}, indent=2))

    if args.window_manifest_out:
        store = ParquetStore(base, snapshots_dataset=output_dataset)
        artifact = build_window_readiness_artifact(
            store,
            min_day=effective_min_day,
            max_day=effective_max_day,
            required_schema_version=required_schema_version,
            min_trading_days=int(args.window_min_trading_days),
            max_gap_days=int(args.window_max_gap_days),
        )
        out_path = Path(args.window_manifest_out)
        write_window_readiness_artifact(out_path, artifact)
        print(json.dumps({"window_manifest_out": str(out_path), "window_readiness": artifact}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

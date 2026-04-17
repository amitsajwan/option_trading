from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from snapshot_app.core.stage_views import project_stage_views_v2_from_flat_row

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None  # type: ignore[assignment]


# Prefixes for morning-context and velocity columns.
# These are computed once per trade_date on the ~11:30 snapshot and must be
# forward-filled to later snapshots of the same day so that MIDDAY/LATE rows
# can use them. Pre-11:30 rows remain null — forward-fill never backfills.
_VELOCITY_FILL_PREFIXES: tuple[str, ...] = ("vel_", "ctx_am_")

DEFAULT_SOURCE_DATASET = "snapshots_ml_flat_v2"
DEFAULT_BASE_DATASET = "market_base"
DEFAULT_STAGE1_DATASET = "stage1_entry_view_v2"
DEFAULT_STAGE2_DATASET = "stage2_direction_view_v2"
DEFAULT_STAGE3_DATASET = "stage3_recipe_view_v2"


def _ensure_duckdb() -> None:
    if duckdb is None:  # pragma: no cover
        raise RuntimeError("duckdb is required. Install with: pip install duckdb")


def _dataset_glob(dataset_root: Path) -> str:
    return (dataset_root / "**" / "*.parquet").as_posix()


def _query_df(sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    _ensure_duckdb()
    con = duckdb.connect(":memory:")
    try:
        return con.execute(sql, params or None).df()
    finally:
        con.close()


def _enumerate_trade_dates(dataset_root: Path, *, start_date: str | None, end_date: str | None) -> list[str]:
    if not dataset_root.exists():
        return []
    where = []
    params: list[Any] = []
    if start_date:
        where.append("trade_date >= ?")
        params.append(str(start_date))
    if end_date:
        where.append("trade_date <= ?")
        params.append(str(end_date))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT DISTINCT trade_date
        FROM read_parquet('{_dataset_glob(dataset_root)}', hive_partitioning=false, union_by_name=true)
        {clause}
        ORDER BY trade_date ASC
    """
    df = _query_df(sql, params or None)
    return df["trade_date"].astype(str).tolist() if len(df) else []


def _load_day_frame(dataset_root: Path, trade_date: str) -> pd.DataFrame:
    sql = f"""
        SELECT *
        FROM read_parquet('{_dataset_glob(dataset_root)}', hive_partitioning=false, union_by_name=true)
        WHERE trade_date = ?
        ORDER BY timestamp ASC
    """
    df = _query_df(sql, [str(trade_date)])
    if len(df) and "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def _existing_output_dates(dataset_root: Path) -> set[str]:
    if not dataset_root.exists():
        return set()
    dates: set[str] = set()
    for path in dataset_root.rglob("*.parquet"):
        if path.stem:
            dates.add(str(path.stem))
    return dates


def _forward_fill_velocity_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill vel_* and ctx_am_* columns within a single trade_date frame.

    These features are computed on the ~11:30 morning snapshot only.
    Forward-fill propagates those values to later snapshots of the same day
    so MIDDAY and LATE_SESSION rows can use them.

    Rules:
    - Frame must be sorted by timestamp ascending before calling.
    - Only forward-fill (ffill). Never backfill.
    - Pre-11:30 rows stay null — they precede the computation and must not see it.
    - Never cross trade_date boundaries (caller is responsible: one day at a time).
    """
    fill_cols = [
        c for c in frame.columns
        if any(c.startswith(prefix) for prefix in _VELOCITY_FILL_PREFIXES)
    ]
    if not fill_cols:
        return frame
    frame = frame.copy()
    frame[fill_cols] = frame[fill_cols].ffill()
    return frame


def _write_day_frame(frame: pd.DataFrame, dataset_root: Path, trade_date: str) -> int:
    year = int(pd.Timestamp(trade_date).year)
    year_dir = dataset_root / f"year={year}"
    year_dir.mkdir(parents=True, exist_ok=True)
    out_path = year_dir / f"{trade_date}.parquet"
    frame.to_parquet(out_path, index=False, engine="pyarrow")
    return int(len(frame))


def _merge_flat_with_base(
    flat_frame: pd.DataFrame,
    base_frame: pd.DataFrame,
) -> pd.DataFrame:
    """
    Enrich flat_frame (snapshots_ml_flat_v2) with v1 computed fields from base_frame (market_base).

    Strategy:
    - flat_frame is the primary/left frame — its snapshot_ids match the oracle exactly.
    - base_frame supplies v1 fields (pcr, realized_vol_30m, vix_current, etc.) that flat lacks.
    - Only columns NOT already in flat_frame are pulled from base_frame.
    - Result has exactly the same rows as flat_frame (left join).
    """
    if base_frame is None or len(base_frame) == 0:
        return flat_frame

    flat_cols = set(flat_frame.columns)
    base_only_cols = [c for c in base_frame.columns if c not in flat_cols]

    if not base_only_cols:
        return flat_frame

    join_key = None
    if "snapshot_id" in flat_frame.columns and "snapshot_id" in base_frame.columns:
        join_key = "snapshot_id"
    elif "timestamp" in flat_frame.columns and "timestamp" in base_frame.columns:
        join_key = "timestamp"

    if join_key is None:
        return flat_frame

    base_subset = base_frame[[join_key] + base_only_cols].copy()
    merged = flat_frame.merge(base_subset, on=join_key, how="left")
    return merged


def _project_day_rows(
    day_frame: pd.DataFrame,
    *,
    build_source: str,
    build_run_id: str,
    output_stage1_dataset: str,
    output_stage2_dataset: str,
    output_stage3_dataset: str,
) -> dict[str, pd.DataFrame]:
    rows = day_frame.sort_values("timestamp").to_dict("records")
    projected: dict[str, list[dict[str, Any]]] = {
        output_stage1_dataset: [],
        output_stage2_dataset: [],
        output_stage3_dataset: [],
    }
    for row in rows:
        trade_date = str(row.get("trade_date") or "")
        year = int(pd.to_numeric(row.get("year"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("year"), errors="coerce")) else int(pd.Timestamp(trade_date).year)
        payloads = project_stage_views_v2_from_flat_row(row)
        mapped = {
            output_stage1_dataset: payloads["stage1_entry_view_v2"],
            output_stage2_dataset: payloads["stage2_direction_view_v2"],
            output_stage3_dataset: payloads["stage3_recipe_view_v2"],
        }
        for dataset_name, payload in mapped.items():
            stage_row = dict(payload)
            stage_row["trade_date"] = trade_date
            stage_row["year"] = year
            stage_row["build_source"] = str(row.get("build_source") or build_source)
            stage_row["build_run_id"] = str(row.get("build_run_id") or build_run_id)
            projected[dataset_name].append(stage_row)
    result: dict[str, pd.DataFrame] = {}
    for dataset_name, items in projected.items():
        df = pd.DataFrame(items)
        if len(df) and "timestamp" in df.columns:
            df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)
            df = _forward_fill_velocity_columns(df)
        result[dataset_name] = df
    return result


def rebuild_stage_views_from_flat(
    *,
    parquet_root: str | Path,
    source_flat_dataset: str = DEFAULT_SOURCE_DATASET,
    base_dataset: str | None = DEFAULT_BASE_DATASET,
    output_stage1_dataset: str = DEFAULT_STAGE1_DATASET,
    output_stage2_dataset: str = DEFAULT_STAGE2_DATASET,
    output_stage3_dataset: str = DEFAULT_STAGE3_DATASET,
    start_date: str | None = None,
    end_date: str | None = None,
    resume: bool = True,
    dry_run: bool = False,
    build_source: str = "historical_v2_rebuild",
    build_run_id: str = "velocity_stage_views_v2",
) -> dict[str, Any]:
    parquet_base = Path(parquet_root)
    source_root = parquet_base / source_flat_dataset

    # Primary source is ALWAYS the flat dataset (snapshots_ml_flat_v2) so that
    # snapshot_ids exactly match the oracle (which is also built from flat).
    # base_dataset (market_base) supplies the v1 computed fields that flat lacks.
    primary_root = source_root
    if not primary_root.exists():
        raise FileNotFoundError(f"primary dataset not found: {primary_root}")

    use_base = False
    if base_dataset:
        base_root = parquet_base / base_dataset
        if base_root.exists():
            use_base = True
        else:
            print(f"[rebuild] base_dataset '{base_dataset}' not found at {base_root}, v1 fields will be NaN")

    all_dates = _enumerate_trade_dates(primary_root, start_date=start_date, end_date=end_date)
    output_roots = {
        output_stage1_dataset: parquet_base / output_stage1_dataset,
        output_stage2_dataset: parquet_base / output_stage2_dataset,
        output_stage3_dataset: parquet_base / output_stage3_dataset,
    }
    already_done = (
        set.intersection(*(_existing_output_dates(root) for root in output_roots.values()))
        if resume and all(root.exists() for root in output_roots.values())
        else set()
    )
    pending_dates = [trade_date for trade_date in all_dates if trade_date not in already_done]

    summary: dict[str, Any] = {
        "status": "dry_run" if dry_run else "complete",
        "parquet_root": str(parquet_base.resolve()),
        "primary_dataset": str(source_flat_dataset),
        "source_flat_dataset": str(source_flat_dataset),
        "base_dataset": str(base_dataset) if base_dataset else None,
        "use_base_for_v1_fields": use_base,
        "output_stage_datasets": {
            "stage1": str(output_stage1_dataset),
            "stage2": str(output_stage2_dataset),
            "stage3": str(output_stage3_dataset),
        },
        "days_available": int(len(all_dates)),
        "days_skipped_existing": int(len(already_done)),
        "days_pending": int(len(pending_dates)),
        "days_processed": 0,
        "rows_written_by_dataset": {
            output_stage1_dataset: 0,
            output_stage2_dataset: 0,
            output_stage3_dataset: 0,
        },
    }
    if dry_run or not pending_dates:
        return summary

    for trade_date in pending_dates:
        # Load primary frame (market_base if available, else flat)
        primary_frame = _load_day_frame(primary_root, trade_date)
        if len(primary_frame) == 0:
            continue

        # Enrich flat frame with v1 fields from base dataset (market_base)
        if use_base:
            base_frame = _load_day_frame(base_root, trade_date)
            day_frame = _merge_flat_with_base(primary_frame, base_frame)
        else:
            day_frame = primary_frame

        projected = _project_day_rows(
            day_frame,
            build_source=build_source,
            build_run_id=build_run_id,
            output_stage1_dataset=output_stage1_dataset,
            output_stage2_dataset=output_stage2_dataset,
            output_stage3_dataset=output_stage3_dataset,
        )
        for dataset_name, frame in projected.items():
            written = _write_day_frame(frame, output_roots[dataset_name], trade_date)
            summary["rows_written_by_dataset"][dataset_name] += int(written)
        summary["days_processed"] += 1
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild versioned stage views from an existing flat parquet dataset.")
    parser.add_argument("--parquet-root", required=True, help="Base parquet root")
    parser.add_argument("--source-flat-dataset", default=DEFAULT_SOURCE_DATASET)
    parser.add_argument(
        "--base-dataset",
        default=DEFAULT_BASE_DATASET,
        help=(
            "Dataset with v1 computed features (e.g. market_base). "
            "Used as the primary source; velocity columns from --source-flat-dataset are merged in by snapshot_id. "
            "Set to empty string to disable and use only the flat dataset."
        ),
    )
    parser.add_argument("--output-stage1-dataset", default=DEFAULT_STAGE1_DATASET)
    parser.add_argument("--output-stage2-dataset", default=DEFAULT_STAGE2_DATASET)
    parser.add_argument("--output-stage3-dataset", default=DEFAULT_STAGE3_DATASET)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build-source", default="historical_v2_rebuild")
    parser.add_argument("--build-run-id", default="velocity_stage_views_v2")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    base_dataset: str | None = args.base_dataset if args.base_dataset else None
    summary = rebuild_stage_views_from_flat(
        parquet_root=args.parquet_root,
        source_flat_dataset=args.source_flat_dataset,
        base_dataset=base_dataset,
        output_stage1_dataset=args.output_stage1_dataset,
        output_stage2_dataset=args.output_stage2_dataset,
        output_stage3_dataset=args.output_stage3_dataset,
        start_date=args.start_date,
        end_date=args.end_date,
        resume=not bool(args.no_resume),
        dry_run=bool(args.dry_run),
        build_source=args.build_source,
        build_run_id=args.build_run_id,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

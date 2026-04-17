from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from snapshot_app.core.snapshot_ml_flat_contract import REQUIRED_COLUMNS_V2
from snapshot_app.core.velocity_features import VELOCITY_COLUMNS

from .catalog.feature_sets import feature_set_specs_by_name
from .contracts.manifests import load_and_resolve_manifest
from .model_search.features import IDENTITY_COLUMNS, LABEL_COLUMNS
from .staged.registries import view_registry

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None  # type: ignore[assignment]


_PLANNED_V2_ENRICHMENT_COLUMNS: tuple[str, ...] = (
    *VELOCITY_COLUMNS,
    "adx_14",
    "vol_spike_ratio",
    "ctx_gap_pct",
    "ctx_gap_up",
    "ctx_gap_down",
)
_NUMERIC_DUCKDB_TYPES = {
    "BIGINT",
    "DOUBLE",
    "FLOAT",
    "HUGEINT",
    "INTEGER",
    "REAL",
    "SMALLINT",
    "TINYINT",
    "UBIGINT",
    "UINTEGER",
    "USMALLINT",
    "UTINYINT",
    "DECIMAL",
}
_NON_FEATURE_COLUMNS = set(IDENTITY_COLUMNS) | set(LABEL_COLUMNS) | {
    "view_name",
    "instrument",
    "schema_name",
    "schema_version",
    "build_source",
    "build_run_id",
    "snapshot_id",
    "timestamp",
    "trade_date",
    "year",
}


def _ensure_duckdb() -> None:
    if duckdb is None:  # pragma: no cover
        raise RuntimeError("duckdb is required. Install with: pip install duckdb")


def _dataset_glob(dataset_root: Path) -> str:
    return (dataset_root / "**" / "*.parquet").as_posix()


def _query_df(sql: str, params: list[Any] | None = None):
    _ensure_duckdb()
    con = duckdb.connect(":memory:")
    try:
        return con.execute(sql, params or None).df()
    finally:
        con.close()


def _query_one(sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    df = _query_df(sql, params)
    return df.iloc[0].to_dict() if len(df) else {}


def _dataset_summary(dataset_root: Path, *, start_date: str, end_date: str) -> dict[str, Any]:
    sql = f"""
        SELECT
            COUNT(*) AS rows,
            COUNT(DISTINCT trade_date) AS trading_days,
            MIN(trade_date) AS first_day,
            MAX(trade_date) AS last_day
        FROM read_parquet('{_dataset_glob(dataset_root)}', hive_partitioning=false, union_by_name=true)
        WHERE trade_date BETWEEN ? AND ?
    """
    row = _query_one(sql, [start_date, end_date])
    return {
        "path": str(dataset_root.resolve()),
        "rows": int(row.get("rows") or 0),
        "trading_days": int(row.get("trading_days") or 0),
        "first_day": row.get("first_day"),
        "last_day": row.get("last_day"),
    }


def _dataset_columns(dataset_root: Path) -> list[str]:
    sql = f"DESCRIBE SELECT * FROM read_parquet('{_dataset_glob(dataset_root)}', hive_partitioning=false, union_by_name=true)"
    df = _query_df(sql)
    return df["column_name"].astype(str).tolist() if len(df) else []


def _dataset_numeric_columns(dataset_root: Path) -> list[str]:
    sql = f"DESCRIBE SELECT * FROM read_parquet('{_dataset_glob(dataset_root)}', hive_partitioning=false, union_by_name=true)"
    df = _query_df(sql)
    if len(df) == 0:
        return []
    mask = df["column_type"].astype(str).str.upper().apply(lambda value: any(value.startswith(prefix) for prefix in _NUMERIC_DUCKDB_TYPES))
    columns = df.loc[mask, "column_name"].astype(str).tolist()
    return [column for column in columns if column not in _NON_FEATURE_COLUMNS]


def _non_null_counts(dataset_root: Path, columns: list[str], *, start_date: str, end_date: str) -> dict[str, int]:
    if not columns:
        return {}
    projections = ", ".join(
        f"SUM(CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END) AS nn__{column}"
        for column in columns
    )
    sql = f"""
        SELECT {projections}
        FROM read_parquet('{_dataset_glob(dataset_root)}', hive_partitioning=false, union_by_name=true)
        WHERE trade_date BETWEEN ? AND ?
    """
    row = _query_one(sql, [start_date, end_date])
    return {column: int(row.get(f"nn__{column}") or 0) for column in columns}


def _key_mismatch_count(source_root: Path, target_root: Path, *, start_date: str, end_date: str) -> int:
    sql = f"""
        WITH source_rows AS (
            SELECT trade_date, timestamp, snapshot_id
            FROM read_parquet('{_dataset_glob(source_root)}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date BETWEEN ? AND ?
        ),
        target_rows AS (
            SELECT trade_date, timestamp, snapshot_id
            FROM read_parquet('{_dataset_glob(target_root)}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date BETWEEN ? AND ?
        )
        SELECT COUNT(*) AS missing_rows
        FROM target_rows t
        LEFT JOIN source_rows s
          ON t.trade_date = s.trade_date
         AND t.timestamp = s.timestamp
         AND t.snapshot_id = s.snapshot_id
        WHERE s.snapshot_id IS NULL
    """
    row = _query_one(sql, [start_date, end_date, start_date, end_date])
    return int(row.get("missing_rows") or 0)


def _resolve_feature_columns(base_columns: list[str], feature_set_name: str) -> list[str]:
    spec = feature_set_specs_by_name()[feature_set_name]
    columns = list(base_columns)
    if spec.include_regex:
        columns = [column for column in columns if any(re.search(pattern, column) for pattern in spec.include_regex)]
    if spec.exclude_regex:
        columns = [column for column in columns if not any(re.search(pattern, column) for pattern in spec.exclude_regex)]
    return columns


def _check_feature_set_missing_rates(
    dataset_root: Path,
    feature_set_name: str,
    resolved_columns: list[str],
    *,
    start_date: str,
    end_date: str,
    missing_rate_max: float = 0.35,
) -> list[str]:
    """Return error strings for any resolved column exceeding missing_rate_max."""
    if not resolved_columns:
        return []
    counts = _non_null_counts(dataset_root, resolved_columns, start_date=start_date, end_date=end_date)
    summary_row = _query_one(
        f"SELECT COUNT(*) AS total FROM read_parquet('{_dataset_glob(dataset_root)}', "
        f"hive_partitioning=false, union_by_name=true) WHERE trade_date BETWEEN ? AND ?",
        [start_date, end_date],
    )
    total = int(summary_row.get("total") or 0)
    if total == 0:
        return []
    errors = []
    for col in resolved_columns:
        nn = counts.get(col, 0)
        missing_rate = 1.0 - (nn / total)
        if missing_rate > missing_rate_max:
            errors.append(
                f"feature_set {feature_set_name}: column '{col}' has {missing_rate:.1%} missing "
                f"(threshold {missing_rate_max:.0%})"
            )
    return errors


def _check_velocity_session_validity(
    dataset_root: Path,
    *,
    start_date: str,
    end_date: str,
) -> list[str]:
    """Session-aware validity check for velocity/morning-context columns.

    Contract:
    - On MIDDAY/LATE_SESSION rows: vel_* and ctx_am_* must be mostly populated (<5% missing).
    - On pre-MIDDAY rows: vel_* and ctx_am_* must remain null (>95% missing).

    Violations indicate either missing forward-fill (first case) or temporal leakage (second case).
    """
    probe_col = "ctx_am_vwap_side"
    glob = _dataset_glob(dataset_root)
    all_cols_df = _query_df(
        f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning=false, union_by_name=true)"
    )
    if probe_col not in all_cols_df.get("column_name", []).tolist():
        return [f"velocity session check skipped: '{probe_col}' not in dataset columns"]

    errors: list[str] = []

    # MIDDAY + LATE_SESSION rows: velocity must be populated
    midday_row = _query_one(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN {probe_col} IS NOT NULL THEN 1 ELSE 0 END) AS nn
        FROM read_parquet('{glob}', hive_partitioning=false, union_by_name=true)
        WHERE trade_date BETWEEN ? AND ?
          AND session_phase IN ('MIDDAY', 'LATE_SESSION')
        """,
        [start_date, end_date],
    )
    midday_total = int(midday_row.get("total") or 0)
    midday_nn = int(midday_row.get("nn") or 0)
    if midday_total > 0:
        midday_missing = 1.0 - (midday_nn / midday_total)
        if midday_missing > 0.05:
            errors.append(
                f"velocity session check FAIL: '{probe_col}' is {midday_missing:.1%} missing on "
                f"MIDDAY/LATE_SESSION rows (expected <5%%). Forward-fill may not have run."
            )

    # Pre-MIDDAY rows: velocity must stay null (no leakage)
    early_row = _query_one(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN {probe_col} IS NOT NULL THEN 1 ELSE 0 END) AS nn
        FROM read_parquet('{glob}', hive_partitioning=false, union_by_name=true)
        WHERE trade_date BETWEEN ? AND ?
          AND session_phase NOT IN ('MIDDAY', 'LATE_SESSION')
        """,
        [start_date, end_date],
    )
    early_total = int(early_row.get("total") or 0)
    early_nn = int(early_row.get("nn") or 0)
    if early_total > 0:
        early_populated = early_nn / early_total
        if early_populated > 0.05:
            errors.append(
                f"velocity session check FAIL: '{probe_col}' is populated on {early_populated:.1%} of "
                f"pre-MIDDAY rows (expected <5%%). This is temporal leakage — backfill must not be used."
            )

    return errors


def run_staged_data_preflight(manifest_path: Path) -> dict[str, Any]:
    resolved = load_and_resolve_manifest(Path(manifest_path), validate_paths=True)
    parquet_root = Path(resolved["inputs"]["parquet_root"]).resolve()
    support_dataset = str(resolved["inputs"]["support_dataset"])
    support_root = parquet_root / support_dataset
    windows = resolved["windows"]
    start_date = min(window["start"] for window in windows.values())
    end_date = max(window["end"] for window in windows.values())

    errors: list[str] = []
    report: dict[str, Any] = {
        "manifest_path": str(Path(manifest_path).resolve()),
        "window_start": start_date,
        "window_end": end_date,
        "support_dataset": support_dataset,
        "datasets": {},
        "stages": {},
        "errors": errors,
    }

    support_columns = _dataset_columns(support_root)
    support_summary = _dataset_summary(support_root, start_date=start_date, end_date=end_date)
    report["datasets"]["support"] = {
        **support_summary,
        "column_count": int(len(support_columns)),
    }
    if support_summary["rows"] <= 0:
        errors.append(f"support dataset has no rows in requested window: {support_dataset}")
    missing_support_columns = [column for column in REQUIRED_COLUMNS_V2 if column not in support_columns]
    if missing_support_columns:
        errors.append(
            "support dataset is not velocity-ready; missing columns: "
            + ", ".join(missing_support_columns[:20])
            + ("" if len(missing_support_columns) <= 20 else f" ... (+{len(missing_support_columns) - 20} more)")
        )
    planned_present = [column for column in _PLANNED_V2_ENRICHMENT_COLUMNS if column in support_columns]
    planned_counts = _non_null_counts(support_root, planned_present, start_date=start_date, end_date=end_date)
    missing_or_empty = [column for column in _PLANNED_V2_ENRICHMENT_COLUMNS if column not in support_columns or planned_counts.get(column, 0) <= 0]
    if missing_or_empty:
        errors.append(
            "support dataset failed planned velocity readiness checks for columns: "
            + ", ".join(missing_or_empty)
        )

    registry = view_registry()
    for stage_name in ("stage1", "stage2", "stage3"):
        view_id = str(resolved["views"][f"{stage_name}_view_id"])
        dataset_name = registry[view_id].dataset_name
        dataset_root = parquet_root / dataset_name
        dataset_columns = _dataset_columns(dataset_root)
        dataset_summary = _dataset_summary(dataset_root, start_date=start_date, end_date=end_date)
        view_missing_from_support = _key_mismatch_count(
            support_root,
            dataset_root,
            start_date=start_date,
            end_date=end_date,
        )
        support_missing_from_view = _key_mismatch_count(
            dataset_root,
            support_root,
            start_date=start_date,
            end_date=end_date,
        )
        numeric_columns = _dataset_numeric_columns(dataset_root)
        feature_sets_report: list[dict[str, Any]] = []
        for feature_set_name in resolved["catalog"]["feature_sets_by_stage"][stage_name]:
            resolved_columns = _resolve_feature_columns(numeric_columns, feature_set_name)
            # Per-feature-set missing rate check: fail if any resolved column exceeds threshold.
            fs_missing_errors = _check_feature_set_missing_rates(
                dataset_root,
                feature_set_name,
                resolved_columns,
                start_date=start_date,
                end_date=end_date,
            )
            errors.extend(fs_missing_errors)
            feature_sets_report.append(
                {
                    "feature_set": feature_set_name,
                    "resolved_column_count": int(len(resolved_columns)),
                    "sample_columns": list(resolved_columns[:20]),
                    "missing_rate_errors": fs_missing_errors,
                }
            )
            if not resolved_columns:
                errors.append(
                    f"{stage_name} feature set {feature_set_name} resolves to zero numeric columns in dataset {dataset_name}"
                )
            if feature_set_name == "fo_velocity_v1" and not any(
                column.startswith("vel_")
                or column.startswith("ctx_am_")
                or column in {"adx_14", "vol_spike_ratio", "ctx_gap_pct", "ctx_gap_up", "ctx_gap_down"}
                for column in resolved_columns
            ):
                errors.append(f"{stage_name} feature set fo_velocity_v1 resolved without any velocity/enrichment columns in {dataset_name}")

        # Session-aware velocity validity check for v2 views.
        if view_id.endswith("_v2"):
            errors.extend(
                _check_velocity_session_validity(dataset_root, start_date=start_date, end_date=end_date)
            )

        planned_view_present = [column for column in _PLANNED_V2_ENRICHMENT_COLUMNS if column in dataset_columns]
        planned_view_counts = _non_null_counts(dataset_root, planned_view_present, start_date=start_date, end_date=end_date)
        empty_view_columns = [column for column in _PLANNED_V2_ENRICHMENT_COLUMNS if column not in dataset_columns or planned_view_counts.get(column, 0) <= 0]
        if view_id.endswith("_v2") and empty_view_columns:
            errors.append(
                f"{stage_name} v2 view dataset {dataset_name} is missing planned enrichment coverage for columns: "
                + ", ".join(empty_view_columns)
            )
        if view_missing_from_support > 0 or support_missing_from_view > 0:
            errors.append(
                f"{stage_name} key parity failed between support dataset {support_dataset} and {dataset_name}: "
                f"view_missing_from_support={view_missing_from_support} support_missing_from_view={support_missing_from_view}"
            )
        report["stages"][stage_name] = {
            "view_id": view_id,
            "dataset_name": dataset_name,
            "summary": dataset_summary,
            "column_count": int(len(dataset_columns)),
            "key_parity": {
                "view_missing_from_support": int(view_missing_from_support),
                "support_missing_from_view": int(support_missing_from_view),
            },
            "feature_sets": feature_sets_report,
        }

    report["status"] = "pass" if not errors else "fail"
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run staged data preflight checks for support/view parity and feature coverage.")
    parser.add_argument("--config", required=True, help="Path to staged manifest")
    parser.add_argument("--output", default=None, help="Optional JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_staged_data_preflight(Path(args.config))
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

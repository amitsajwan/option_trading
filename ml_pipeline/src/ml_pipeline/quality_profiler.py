import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .raw_loader import filter_valid_options, load_day_raw_data
from .schema_validator import DEFAULT_REPRESENTATIVE_DAYS, resolve_archive_base


NUMERIC_BY_DATASET: Dict[str, List[str]] = {
    "fut": ["open", "high", "low", "close", "oi", "volume"],
    "options": ["open", "high", "low", "close", "oi", "volume", "strike"],
    "spot": ["open", "high", "low", "close"],
}
PRIMARY_KEYS: Dict[str, List[str]] = {
    "fut": ["timestamp"],
    "options": ["timestamp", "symbol"],
    "spot": ["timestamp"],
}


@dataclass(frozen=True)
class DatasetQuality:
    dataset: str
    rows: int
    missing_cells: int
    duplicate_rows: int
    outlier_rows: int
    missing_timestamps: int
    invalid_symbol_rows: int = 0


def count_missing_values(df: pd.DataFrame, columns: Sequence[str]) -> int:
    if df.empty:
        return 0
    subset = df.loc[:, list(columns)]
    return int(subset.isna().sum().sum())


def count_duplicates(df: pd.DataFrame, key_columns: Sequence[str]) -> int:
    if df.empty:
        return 0
    return int(df.duplicated(subset=list(key_columns), keep=False).sum())


def detect_iqr_outliers(df: pd.DataFrame, numeric_columns: Sequence[str], multiplier: float = 3.0) -> int:
    if df.empty:
        return 0
    outlier_mask = pd.Series(False, index=df.index)
    for col in numeric_columns:
        series = pd.to_numeric(df[col], errors="coerce")
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        if pd.isna(iqr) or iqr == 0:
            continue
        lo = q1 - (multiplier * iqr)
        hi = q3 + (multiplier * iqr)
        outlier_mask = outlier_mask | ((series < lo) | (series > hi))
    return int(outlier_mask.sum())


def profile_dataset(df: pd.DataFrame, dataset: str) -> DatasetQuality:
    numeric_columns = NUMERIC_BY_DATASET[dataset]
    key_columns = PRIMARY_KEYS[dataset]
    base_missing = count_missing_values(df, numeric_columns)
    duplicate_rows = count_duplicates(df, key_columns)
    outlier_rows = detect_iqr_outliers(df, numeric_columns)
    missing_timestamps = int(df["timestamp"].isna().sum()) if "timestamp" in df.columns else len(df)
    invalid_symbol_rows = 0
    if dataset == "options":
        invalid_symbol_rows = int((~df["option_type"].isin(["CE", "PE"])).sum())
    return DatasetQuality(
        dataset=dataset,
        rows=int(len(df)),
        missing_cells=base_missing,
        duplicate_rows=duplicate_rows,
        outlier_rows=outlier_rows,
        missing_timestamps=missing_timestamps,
        invalid_symbol_rows=invalid_symbol_rows,
    )


def profile_day(base_path: Path, day: str) -> Dict[str, object]:
    raw = load_day_raw_data(base_path=base_path, day=day)
    fut_profile = profile_dataset(raw.fut, "fut")
    options_profile = profile_dataset(raw.options, "options")
    spot_profile = profile_dataset(raw.spot, "spot")
    valid_options = filter_valid_options(raw.options)
    return {
        "day": day,
        "datasets": {
            "fut": fut_profile.__dict__,
            "options": options_profile.__dict__,
            "spot": spot_profile.__dict__,
        },
        "derived": {
            "valid_options_rows": int(len(valid_options)),
            "valid_options_ratio": (float(len(valid_options)) / float(len(raw.options))) if len(raw.options) else 0.0,
            "fut_unique_timestamps": int(raw.fut["timestamp"].nunique()),
            "spot_unique_timestamps": int(raw.spot["timestamp"].nunique()),
            "options_unique_minutes": int(raw.options["timestamp"].nunique()),
        },
    }


def profile_days(base_path: Path, days: Sequence[str]) -> Dict[str, object]:
    day_profiles = [profile_day(base_path=base_path, day=day) for day in days]
    totals = {
        "days_total": len(days),
        "rows_total": 0,
        "missing_cells_total": 0,
        "duplicates_total": 0,
        "outliers_total": 0,
        "missing_timestamps_total": 0,
    }
    for item in day_profiles:
        for dataset in ("fut", "options", "spot"):
            stats = item["datasets"][dataset]
            totals["rows_total"] += int(stats["rows"])
            totals["missing_cells_total"] += int(stats["missing_cells"])
            totals["duplicates_total"] += int(stats["duplicate_rows"])
            totals["outliers_total"] += int(stats["outlier_rows"])
            totals["missing_timestamps_total"] += int(stats["missing_timestamps"])
    return {
        "base_path": str(base_path),
        "days": list(days),
        "totals": totals,
        "days_profile": day_profiles,
    }


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_markdown(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = payload["totals"]
    lines: List[str] = []
    lines.append("# Data Quality Summary (T02)")
    lines.append("")
    lines.append(f"- Base path: `{payload['base_path']}`")
    lines.append(f"- Days: `{', '.join(payload['days'])}`")
    lines.append(f"- Total rows: `{totals['rows_total']}`")
    lines.append(f"- Missing cells: `{totals['missing_cells_total']}`")
    lines.append(f"- Duplicate rows: `{totals['duplicates_total']}`")
    lines.append(f"- Outlier rows: `{totals['outliers_total']}`")
    lines.append(f"- Missing timestamps: `{totals['missing_timestamps_total']}`")
    lines.append("")
    lines.append("## Per-Day Snapshot")
    lines.append("")
    for item in payload["days_profile"]:
        day = item["day"]
        fut_rows = item["datasets"]["fut"]["rows"]
        opt_rows = item["datasets"]["options"]["rows"]
        spot_rows = item["datasets"]["spot"]["rows"]
        valid_ratio = item["derived"]["valid_options_ratio"]
        lines.append(
            f"- `{day}`: fut={fut_rows}, options={opt_rows}, spot={spot_rows}, "
            f"valid_options_ratio={valid_ratio:.4f}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _split_days(raw_days: Optional[str]) -> List[str]:
    if not raw_days:
        return list(DEFAULT_REPRESENTATIVE_DAYS)
    return [item.strip() for item in raw_days.split(",") if item.strip()]


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Profile raw data quality for BankNifty archive")
    parser.add_argument("--base-path", default=None, help="Archive base path")
    parser.add_argument("--days", default=None, help="Comma separated days (YYYY-MM-DD)")
    parser.add_argument(
        "--json-out",
        default="ml_pipeline/artifacts/t02_data_quality_report.json",
        help="JSON report output",
    )
    parser.add_argument(
        "--summary-out",
        default="ml_pipeline/artifacts/t02_data_quality_summary.md",
        help="Markdown summary output",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    base = resolve_archive_base(explicit_base=args.base_path)
    if base is None:
        print("ERROR: archive base path not found")
        return 2

    days = _split_days(args.days)
    if not days:
        print("ERROR: no days provided")
        return 2

    payload = profile_days(base_path=base, days=days)
    json_out = Path(args.json_out)
    summary_out = Path(args.summary_out)
    _write_json(json_out, payload)
    _write_markdown(summary_out, payload)

    totals = payload["totals"]
    print(f"Base path: {base}")
    print(f"Days: {totals['days_total']}")
    print(f"Rows total: {totals['rows_total']}")
    print(f"Missing cells: {totals['missing_cells_total']}")
    print(f"Duplicates: {totals['duplicates_total']}")
    print(f"Outliers: {totals['outliers_total']}")
    print(f"JSON: {json_out}")
    print(f"Summary: {summary_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())


import argparse
import csv
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


DATASET_TYPES: Tuple[str, ...] = ("fut", "options", "spot")
DEFAULT_REPRESENTATIVE_DAYS: Tuple[str, ...] = (
    "2020-01-03",
    "2021-06-15",
    "2022-12-01",
    "2023-06-15",
    "2024-10-10",
)
DEFAULT_ARCHIVE_CANDIDATES: Tuple[str, ...] = (
    r"C:\Users\amits\Downloads\archive\banknifty_data",
    r"C:\archive\banknifty_data",
)


REQUIRED_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "fut": ("date", "time", "symbol", "open", "high", "low", "close", "oi", "volume"),
    "options": ("date", "time", "symbol", "open", "high", "low", "close", "oi", "volume"),
    "spot": ("date", "time", "symbol", "open", "high", "low", "close"),
}

NUMERIC_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "fut": ("open", "high", "low", "close", "oi", "volume"),
    "options": ("open", "high", "low", "close", "oi", "volume"),
    "spot": ("open", "high", "low", "close"),
}


@dataclass
class FileValidationResult:
    dataset: str
    path: str
    exists: bool
    rows: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exists and not self.errors


@dataclass
class DayValidationResult:
    date: str
    files: List[FileValidationResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.files)


@dataclass
class ValidationReport:
    base_path: str
    dates: List[str]
    results: List[DayValidationResult]

    def to_dict(self) -> Dict[str, object]:
        pass_count = sum(1 for day in self.results if day.ok)
        fail_count = len(self.results) - pass_count
        return {
            "base_path": self.base_path,
            "dates": self.dates,
            "summary": {
                "days_total": len(self.results),
                "pass_count": pass_count,
                "fail_count": fail_count,
            },
            "results": [asdict(day) for day in self.results],
        }


def resolve_archive_base(explicit_base: Optional[str] = None) -> Optional[Path]:
    candidates: List[str] = []
    if explicit_base:
        candidates.append(explicit_base)
    env_base = os.getenv("LOCAL_HISTORICAL_BASE", "").strip()
    if env_base:
        candidates.append(env_base)
    candidates.extend(DEFAULT_ARCHIVE_CANDIDATES)
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def parse_day(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d")


def build_file_path(base_path: Path, dataset: str, day: str) -> Path:
    day_dt = parse_day(day)
    year = str(day_dt.year)
    month = str(day_dt.month)
    dd_mm_yyyy = day_dt.strftime("%d_%m_%Y")
    if dataset == "fut":
        return base_path / "banknifty_fut" / year / month / f"banknifty_fut_{dd_mm_yyyy}.csv"
    if dataset == "options":
        return base_path / "banknifty_options" / year / month / f"banknifty_options_{dd_mm_yyyy}.csv"
    if dataset == "spot":
        return base_path / "banknifty_spot" / year / month / f"banknifty_spot{dd_mm_yyyy}.csv"
    raise ValueError(f"Unsupported dataset: {dataset}")


def discover_available_days(base_path: Path) -> List[str]:
    fut_root = base_path / "banknifty_fut"
    opt_root = base_path / "banknifty_options"
    spot_root = base_path / "banknifty_spot"
    if not (fut_root.exists() and opt_root.exists() and spot_root.exists()):
        return []

    fut_days: Set[str] = set()
    opt_days: Set[str] = set()
    spot_days: Set[str] = set()

    fut_pat = re.compile(r"^banknifty_fut_(\d{2})_(\d{2})_(\d{4})\.csv$", re.IGNORECASE)
    opt_pat = re.compile(r"^banknifty_options_(\d{2})_(\d{2})_(\d{4})\.csv$", re.IGNORECASE)
    spot_pat = re.compile(r"^banknifty_spot(\d{2})_(\d{2})_(\d{4})\.csv$", re.IGNORECASE)

    def to_day(m: re.Match) -> str:
        dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{yyyy}-{mm}-{dd}"

    for path in fut_root.rglob("*.csv"):
        m = fut_pat.match(path.name)
        if m:
            fut_days.add(to_day(m))
    for path in opt_root.rglob("*.csv"):
        m = opt_pat.match(path.name)
        if m:
            opt_days.add(to_day(m))
    for path in spot_root.rglob("*.csv"):
        m = spot_pat.match(path.name)
        if m:
            spot_days.add(to_day(m))

    common = sorted(fut_days & opt_days & spot_days)
    return common


def _parse_timestamp(date_raw: str, time_raw: str) -> Optional[datetime]:
    text = f"{str(date_raw).strip()} {str(time_raw).strip()}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_file(path: Path, dataset: str) -> FileValidationResult:
    result = FileValidationResult(dataset=dataset, path=str(path), exists=path.exists())
    if not path.exists():
        result.errors.append("File not found")
        return result

    required_columns = REQUIRED_COLUMNS[dataset]
    numeric_columns = NUMERIC_COLUMNS[dataset]

    seen_primary: set = set()
    duplicate_count = 0
    bad_timestamp_count = 0
    bad_numeric_count = 0

    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            result.errors.append("CSV header is missing")
            return result

        actual_columns = tuple(reader.fieldnames)
        missing = [col for col in required_columns if col not in actual_columns]
        if missing:
            result.errors.append(f"Missing required columns: {','.join(missing)}")
            return result

        for row in reader:
            result.rows += 1
            ts = _parse_timestamp(row.get("date", ""), row.get("time", ""))
            if ts is None:
                bad_timestamp_count += 1

            for col in numeric_columns:
                if _to_float(row.get(col, "")) is None:
                    bad_numeric_count += 1
                    break

            if dataset in {"fut", "spot"}:
                primary_key = (row.get("date", ""), row.get("time", ""))
            else:
                primary_key = (row.get("date", ""), row.get("time", ""), row.get("symbol", ""))
            if primary_key in seen_primary:
                duplicate_count += 1
            else:
                seen_primary.add(primary_key)

    if result.rows == 0:
        result.errors.append("CSV has no rows")

    if bad_timestamp_count:
        result.errors.append(f"Invalid date/time rows: {bad_timestamp_count}")
    if bad_numeric_count:
        result.errors.append(f"Invalid numeric rows: {bad_numeric_count}")
    if duplicate_count:
        result.errors.append(f"Duplicate primary key rows: {duplicate_count}")

    if dataset == "options" and result.rows > 0:
        if result.rows < 1000:
            result.warnings.append("Options rows unusually low for a full day")
    if dataset in {"fut", "spot"} and result.rows > 0:
        if result.rows < 300:
            result.warnings.append("Minute bars lower than expected full session")

    return result


def validate_day(base_path: Path, day: str) -> DayValidationResult:
    day_result = DayValidationResult(date=day)
    for dataset in DATASET_TYPES:
        file_path = build_file_path(base_path=base_path, dataset=dataset, day=day)
        day_result.files.append(validate_file(path=file_path, dataset=dataset))
    return day_result


def validate_days(base_path: Path, days: Sequence[str]) -> ValidationReport:
    results = [validate_day(base_path=base_path, day=day) for day in days]
    return ValidationReport(base_path=str(base_path), dates=list(days), results=results)


def _split_days(days_raw: Optional[str]) -> List[str]:
    if not days_raw:
        return list(DEFAULT_REPRESENTATIVE_DAYS)
    values = [item.strip() for item in days_raw.split(",")]
    return [item for item in values if item]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate BankNifty archive schema contract")
    parser.add_argument("--base-path", default=None, help="Archive base path")
    parser.add_argument(
        "--days",
        default=None,
        help="Comma-separated date list (YYYY-MM-DD). Defaults to representative set.",
    )
    parser.add_argument(
        "--out",
        default="ml_pipeline/artifacts/t01_schema_validation_report.json",
        help="Output JSON report path",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    base = resolve_archive_base(explicit_base=args.base_path)
    if base is None:
        print("ERROR: could not resolve archive base path")
        return 2

    days = _split_days(args.days)
    if not days:
        print("ERROR: empty day list")
        return 2

    report = validate_days(base_path=base, days=days)
    report_dict = report.to_dict()
    out_path = Path(args.out)
    _ensure_parent(out_path)
    out_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")

    summary = report_dict["summary"]
    print(f"Base path: {base}")
    print(f"Days total: {summary['days_total']}")
    print(f"Passed: {summary['pass_count']}")
    print(f"Failed: {summary['fail_count']}")
    print(f"Report: {out_path}")

    return 0 if summary["fail_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())

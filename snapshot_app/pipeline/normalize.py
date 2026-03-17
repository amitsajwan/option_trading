from __future__ import annotations

import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore

from .config import (
    DEFAULT_NORMALIZE_JOBS,
    DEFAULT_PARQUET_BASE,
    DEFAULT_RAW_DATA_ROOT,
    NormalizeTask,
    PARTITIONED_DATASETS,
    RAW_DATASET_DIRS,
)


def _safe_float_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(col).strip().lower().replace(" ", "_") for col in out.columns]
    return out


def _resolve_timestamp(frame: pd.DataFrame) -> pd.Series:
    if "timestamp" in frame.columns:
        return pd.to_datetime(frame["timestamp"], errors="coerce")
    date_col = None
    for candidate in ("date", "trade_date"):
        if candidate in frame.columns:
            date_col = candidate
            break
    time_col = "time" if "time" in frame.columns else None
    if date_col is None:
        return pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    if time_col is not None:
        return pd.to_datetime(
            frame[date_col].astype(str).str.strip() + " " + frame[time_col].astype(str).str.strip(),
            errors="coerce",
        )
    return pd.to_datetime(frame[date_col], errors="coerce")


def _safe_text_series(frame: pd.DataFrame, column: str, *, default: str = "") -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="object")
    return frame[column].astype(str)


def _normalize_partition_frame(*, dataset: str, frame: pd.DataFrame) -> pd.DataFrame:
    work = _normalize_columns(frame)
    work["timestamp"] = _resolve_timestamp(work)
    work = work.dropna(subset=["timestamp"]).copy()
    if len(work) == 0:
        return pd.DataFrame()
    work["trade_date"] = work["timestamp"].dt.strftime("%Y-%m-%d")
    work["symbol"] = _safe_text_series(work, "symbol").str.strip().str.upper()
    for column in ("open", "high", "low", "close", "volume", "oi"):
        work[column] = _safe_float_series(work, column)

    base_columns = ["timestamp", "trade_date", "symbol", "open", "high", "low", "close"]
    if dataset == "futures":
        out = work.loc[:, base_columns + ["volume", "oi"]].copy()
    elif dataset == "spot":
        out = work.loc[:, base_columns].copy()
    elif dataset == "options":
        normalized = ParquetStore._normalize_option_fields(work)
        normalized["strike"] = pd.to_numeric(normalized.get("strike"), errors="coerce")
        normalized["option_type"] = _safe_text_series(normalized, "option_type").str.strip().str.upper()
        normalized["expiry_str"] = _safe_text_series(normalized, "expiry_str").str.strip().str.upper()
        if "iv" in normalized.columns:
            normalized["iv"] = pd.to_numeric(normalized["iv"], errors="coerce")
        else:
            normalized["iv"] = pd.NA
        out = normalized.loc[
            :,
            base_columns + ["volume", "oi", "strike", "option_type", "expiry_str", "iv"],
        ].copy()
    else:
        raise ValueError(f"unsupported dataset={dataset}")

    out = out.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    return out


def _iter_csv_files(source_dir: Path) -> list[Path]:
    return sorted(path for path in source_dir.glob("*.csv") if path.is_file())


def _read_partition_csvs(csv_paths: Iterable[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in csv_paths:
        frame = pd.read_csv(path)
        if len(frame) == 0:
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=0, ignore_index=True)


def _normalize_partition_task(task: NormalizeTask, *, force: bool) -> dict[str, Any]:
    if task.output_path.exists() and not force:
        return {
            "dataset": task.dataset,
            "year": int(task.year),
            "month": int(task.month),
            "status": "skipped_existing",
            "output_path": str(task.output_path),
            "rows": None,
            "source_files": len(_iter_csv_files(task.source_dir)),
        }
    csv_paths = _iter_csv_files(task.source_dir)
    if not csv_paths:
        return {
            "dataset": task.dataset,
            "year": int(task.year),
            "month": int(task.month),
            "status": "no_files",
            "output_path": str(task.output_path),
            "rows": 0,
            "source_files": 0,
        }
    raw = _read_partition_csvs(csv_paths)
    normalized = _normalize_partition_frame(dataset=task.dataset, frame=raw)
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_parquet(task.output_path, index=False, compression="snappy")
    return {
        "dataset": task.dataset,
        "year": int(task.year),
        "month": int(task.month),
        "status": "written",
        "output_path": str(task.output_path),
        "rows": int(len(normalized)),
        "source_files": len(csv_paths),
    }


def _normalize_partition_task_entry(payload: tuple[NormalizeTask, bool]) -> dict[str, Any]:
    task, force = payload
    return _normalize_partition_task(task, force=force)


def discover_normalize_tasks(
    *,
    raw_root: Path,
    parquet_base: Path,
    datasets: Optional[Iterable[str]] = None,
) -> list[NormalizeTask]:
    selected = [str(name).strip().lower() for name in (datasets or PARTITIONED_DATASETS)]
    invalid = sorted(name for name in selected if name not in PARTITIONED_DATASETS)
    if invalid:
        raise ValueError(f"unsupported raw datasets: {', '.join(invalid)}")

    tasks: list[NormalizeTask] = []
    for dataset in selected:
        dataset_root = raw_root / RAW_DATASET_DIRS[dataset]
        if not dataset_root.exists():
            continue
        for year_dir in sorted(path for path in dataset_root.iterdir() if path.is_dir()):
            if not year_dir.name.isdigit():
                continue
            year = int(year_dir.name)
            for month_dir in sorted(path for path in year_dir.iterdir() if path.is_dir()):
                if not month_dir.name.isdigit():
                    continue
                month = int(month_dir.name)
                output_path = (
                    parquet_base
                    / dataset
                    / f"year={year:04d}"
                    / f"month={month:02d}"
                    / "data.parquet"
                )
                tasks.append(
                    NormalizeTask(
                        dataset=dataset,
                        year=year,
                        month=month,
                        source_dir=month_dir,
                        output_path=output_path,
                    )
                )
    return tasks


def _discover_vix_csvs(*, raw_root: Path, vix_root: Optional[Path]) -> list[Path]:
    roots: list[Path] = []
    if vix_root is not None:
        roots.append(vix_root)
    roots.append(raw_root)
    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.csv"):
            lowered = path.name.lower()
            if "vix" not in lowered:
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return sorted(files)


def _normalize_vix_frame(frame: pd.DataFrame) -> pd.DataFrame:
    work = _normalize_columns(frame)
    rename_map = {
        "prev._close": "prev_close",
        "prev.close": "prev_close",
        "prev._close_": "prev_close",
        "prev._close__": "prev_close",
        "prev._close___": "prev_close",
        "prev._close____": "prev_close",
        "prev_close": "prev_close",
    }
    work = work.rename(columns=rename_map)
    date_col = "date" if "date" in work.columns else "trade_date"
    if date_col not in work.columns:
        return pd.DataFrame()
    raw_dates = work[date_col].astype(str).str.strip()
    trade_date = pd.to_datetime(raw_dates, format="%d-%b-%Y", errors="coerce")
    if trade_date.isna().all():
        trade_date = pd.to_datetime(raw_dates, errors="coerce", dayfirst=True)
    work["trade_date"] = trade_date.dt.strftime("%Y-%m-%d")
    work = work.dropna(subset=["trade_date"]).copy()
    work["vix_open"] = _safe_float_series(work, "open")
    work["vix_high"] = _safe_float_series(work, "high")
    work["vix_low"] = _safe_float_series(work, "low")
    work["vix_close"] = _safe_float_series(work, "close")
    work["vix_prev_close"] = _safe_float_series(work, "prev_close")
    out = work.loc[:, ["trade_date", "vix_open", "vix_high", "vix_low", "vix_close", "vix_prev_close"]].copy()
    out = out.drop_duplicates(subset=["trade_date"], keep="last").sort_values("trade_date").reset_index(drop=True)
    return out


def normalize_vix_to_parquet(
    *,
    raw_root: Path,
    parquet_base: Path,
    vix_root: Optional[Path] = None,
    force: bool = False,
) -> dict[str, Any]:
    output_path = parquet_base / "vix" / "vix.parquet"
    if output_path.exists() and not force:
        return {
            "status": "skipped_existing",
            "output_path": str(output_path),
            "rows": None,
            "source_files": 0,
        }
    csv_paths = _discover_vix_csvs(raw_root=raw_root, vix_root=vix_root)
    if not csv_paths:
        return {
            "status": "missing",
            "output_path": str(output_path),
            "rows": 0,
            "source_files": 0,
        }
    raw = _read_partition_csvs(csv_paths)
    normalized = _normalize_vix_frame(raw)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_parquet(output_path, index=False, compression="snappy")
    return {
        "status": "written",
        "output_path": str(output_path),
        "rows": int(len(normalized)),
        "source_files": len(csv_paths),
    }


def normalize_raw_to_parquet(
    *,
    raw_root: str | Path = DEFAULT_RAW_DATA_ROOT,
    parquet_base: str | Path = DEFAULT_PARQUET_BASE,
    vix_root: str | Path | None = None,
    datasets: Optional[Iterable[str]] = None,
    jobs: int = DEFAULT_NORMALIZE_JOBS,
    force: bool = False,
) -> dict[str, Any]:
    resolved_raw_root = Path(raw_root)
    if not resolved_raw_root.exists():
        raise FileNotFoundError(f"raw data root not found: {resolved_raw_root}")
    resolved_parquet_base = Path(parquet_base)
    resolved_parquet_base.mkdir(parents=True, exist_ok=True)
    resolved_vix_root = Path(vix_root) if vix_root is not None else None

    tasks = discover_normalize_tasks(
        raw_root=resolved_raw_root,
        parquet_base=resolved_parquet_base,
        datasets=datasets,
    )
    task_payloads = [(task, bool(force)) for task in tasks]
    partition_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    max_workers = max(1, min(int(jobs), max(1, len(task_payloads)))) if task_payloads else 1
    if task_payloads:
        if max_workers == 1:
            for payload in task_payloads:
                task = payload[0]
                try:
                    partition_results.append(_normalize_partition_task_entry(payload))
                except Exception as exc:
                    errors.append(
                        {
                            "dataset": task.dataset,
                            "year": int(task.year),
                            "month": int(task.month),
                            "error": str(exc),
                        }
                    )
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_normalize_partition_task_entry, payload): payload[0]
                    for payload in task_payloads
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        partition_results.append(future.result())
                    except Exception as exc:
                        errors.append(
                            {
                                "dataset": task.dataset,
                                "year": int(task.year),
                                "month": int(task.month),
                                "error": str(exc),
                            }
                        )

    vix_result = normalize_vix_to_parquet(
        raw_root=resolved_raw_root,
        parquet_base=resolved_parquet_base,
        vix_root=resolved_vix_root,
        force=force,
    )

    rows_written = sum(int(row.get("rows") or 0) for row in partition_results if row.get("status") == "written")
    rows_written += int(vix_result.get("rows") or 0) if vix_result.get("status") == "written" else 0
    return {
        "status": "complete" if not errors else "partial_error",
        "raw_root": str(resolved_raw_root),
        "parquet_base": str(resolved_parquet_base),
        "jobs": int(max_workers),
        "tasks_total": int(len(task_payloads)),
        "tasks_written": int(sum(1 for row in partition_results if row.get("status") == "written")),
        "tasks_skipped_existing": int(sum(1 for row in partition_results if row.get("status") == "skipped_existing")),
        "tasks_no_files": int(sum(1 for row in partition_results if row.get("status") == "no_files")),
        "rows_written": int(rows_written),
        "partition_results": sorted(
            partition_results,
            key=lambda row: (str(row.get("dataset")), int(row.get("year") or 0), int(row.get("month") or 0)),
        ),
        "vix_result": vix_result,
        "errors": errors,
    }

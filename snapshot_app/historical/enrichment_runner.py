"""
EnrichmentBatchRunner — backfill velocity features onto snapshots_ml_flat.

For every trade_date in the requested range:
  1. Load morning session snapshots (10:00–11:30) from snapshots_ml_flat
     + IV from raw snapshots JSON (atm_ce_iv, atm_pe_iv, iv_skew).
  2. Identify the 11:30 row (midday_snapshot).
  3. Compute ~30 velocity features via compute_velocity_features().
  4. Merge velocity features into ALL rows for that date:
       - 11:30 row:   velocity features are populated.
       - Other rows:  velocity features are NaN (consistent schema).
  5. Write the enriched rows to snapshots_ml_flat_v2/year=YYYY/data.parquet.

The run is resume-safe: dates already present in the output are skipped.

CLI usage:
  python -m snapshot_app.historical.enrichment_runner \\
      --parquet-root /path/to/parquet_data \\
      --start-date 2020-01-01 \\
      --end-date 2024-12-31 \\
      --output-dataset snapshots_ml_flat_v2 \\
      [--dry-run] \\
      [--workers 4] \\
      [--log-level INFO]
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from snapshot_app.core.velocity_features import (
    VELOCITY_COLUMNS,
    VELOCITY_INTEGER_COLUMNS,
    compute_velocity_features,
)
from snapshot_app.historical.morning_session import MorningSessionLoader

logger = logging.getLogger(__name__)

# ── midday target: 11:30 IST ───────────────────────────────────────────────────
_MIDDAY_HOUR = 11
_MIDDAY_MIN = 30

# ── output constants ───────────────────────────────────────────────────────────
DEFAULT_OUTPUT_DATASET = "snapshots_ml_flat_v2"
DEFAULT_ML_FLAT_DATASET = "snapshots_ml_flat"
DEFAULT_RAW_DATASET = "snapshots"
SCHEMA_VERSION_V2 = "4.0"

# ── logging cadence ────────────────────────────────────────────────────────────
_LOG_EVERY_N_DATES = 50


# ────────────────────────────────────────────────────────────────────────────────

@dataclass
class EnrichmentResult:
    trade_date: str
    status: str          # "enriched" | "skipped" | "no_midday_row" | "no_morning_data" | "error"
    rows_written: int = 0
    error: str = ""
    velocity_nan_count: int = 0  # how many velocity cols were NaN on the 11:30 row


@dataclass
class RunSummary:
    total_dates: int = 0
    enriched: int = 0
    skipped: int = 0
    no_midday: int = 0
    no_morning_data: int = 0
    errors: int = 0
    rows_written: int = 0
    duration_seconds: float = 0.0
    failed_dates: List[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────

def _find_midday_row(full_day_df: pd.DataFrame) -> Optional[pd.Series]:
    """Return the 11:30 row from a full-day ml_flat DataFrame, or None."""
    if len(full_day_df) == 0:
        return None
    ts = pd.to_datetime(full_day_df["timestamp"], errors="coerce")
    mask = (ts.dt.hour == _MIDDAY_HOUR) & (ts.dt.minute == _MIDDAY_MIN)
    matches = full_day_df[mask]
    if len(matches) == 0:
        return None
    return matches.iloc[-1]   # take the last one if duplicates


def _load_full_day_ml_flat(trade_date: str, ml_flat_root: Path) -> pd.DataFrame:
    """Load ALL ml_flat rows for trade_date (not just morning window)."""
    try:
        import duckdb
    except ImportError:
        logger.error("duckdb not installed")
        return pd.DataFrame()

    glob_pattern = (ml_flat_root / "**" / "*.parquet").as_posix()
    try:
        con = duckdb.connect(":memory:")
        df: pd.DataFrame = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date = ?
            ORDER BY timestamp ASC
            """,
            [trade_date],
        ).df()
        con.close()
    except Exception as exc:
        logger.warning("full day ml_flat load failed for %s: %s", trade_date, exc)
        return pd.DataFrame()

    if len(df) > 0:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def _get_already_processed_dates(output_root: Path) -> Set[str]:
    """Return set of trade_dates already written to the output dataset."""
    if not output_root.exists():
        return set()
    try:
        import duckdb
        glob_pattern = (output_root / "**" / "*.parquet").as_posix()
        con = duckdb.connect(":memory:")
        df: pd.DataFrame = con.execute(
            f"""
            SELECT DISTINCT trade_date
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            """
        ).df()
        con.close()
        return set(df["trade_date"].astype(str).tolist())
    except Exception:
        return set()


def _get_prev_day_close(
    trade_date: str,
    all_dates_sorted: List[str],
    ml_flat_root: Path,
) -> Optional[float]:
    """Return previous trading day's final futures close price."""
    idx = all_dates_sorted.index(trade_date) if trade_date in all_dates_sorted else -1
    if idx <= 0:
        return None
    prev_date = all_dates_sorted[idx - 1]

    try:
        import duckdb
        glob_pattern = (ml_flat_root / "**" / "*.parquet").as_posix()
        con = duckdb.connect(":memory:")
        df: pd.DataFrame = con.execute(
            f"""
            SELECT px_fut_close
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            [prev_date],
        ).df()
        con.close()
    except Exception:
        return None

    if len(df) == 0:
        return None
    val = pd.to_numeric(df["px_fut_close"].iloc[0], errors="coerce")
    return float(val) if pd.notna(val) else None


def _load_midday_option_volume_lookup(ml_flat_root: Path) -> Dict[str, float]:
    """Return trade_date -> 11:30 total options volume from the source ml_flat dataset."""
    try:
        import duckdb
    except ImportError:
        logger.error("duckdb not installed")
        return {}

    glob_pattern = (ml_flat_root / "**" / "*.parquet").as_posix()
    try:
        con = duckdb.connect(":memory:")
        df: pd.DataFrame = con.execute(
            f"""
            SELECT trade_date, opt_flow_options_volume_total
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            WHERE EXTRACT(hour FROM timestamp) = 11
              AND EXTRACT(minute FROM timestamp) = 30
            ORDER BY trade_date ASC
            """
        ).df()
        con.close()
    except Exception as exc:
        logger.warning("midday option volume lookup load failed: %s", exc)
        return {}

    if len(df) == 0:
        return {}

    lookup: Dict[str, float] = {}
    for _, row in df.iterrows():
        value = pd.to_numeric(row.get("opt_flow_options_volume_total"), errors="coerce")
        if pd.notna(value):
            lookup[str(row["trade_date"])] = float(value)
    return lookup


def _compute_volume_context(
    trade_date: str,
    all_dates_sorted: List[str],
    midday_option_volume_by_date: Dict[str, float],
) -> Tuple[Optional[float], Optional[float]]:
    """Return (previous_day_midday_volume, trailing_20d_midday_volume_avg)."""
    if trade_date not in all_dates_sorted:
        return None, None
    idx = all_dates_sorted.index(trade_date)
    if idx <= 0:
        return None, None

    previous_days = all_dates_sorted[:idx]
    prev_day = previous_days[-1]
    prev_value = midday_option_volume_by_date.get(prev_day)

    trailing_values = [
        float(midday_option_volume_by_date[day])
        for day in previous_days[-20:]
        if day in midday_option_volume_by_date and pd.notna(midday_option_volume_by_date[day])
    ]
    avg_20d = float(sum(trailing_values) / len(trailing_values)) if trailing_values else None
    return prev_value, avg_20d


def _attach_velocity_to_day(
    full_day_df: pd.DataFrame,
    velocity: Dict[str, float],
    *,
    schema_version: str = SCHEMA_VERSION_V2,
) -> pd.DataFrame:
    """
    Merge velocity dict onto all rows for a trade_date.
    11:30 row gets populated values; all other rows get NaN for velocity columns.
    Also stamps schema_version to v2 on the 11:30 row only — other rows keep
    original schema_version so both generations coexist in the dataset.
    """
    df = full_day_df.copy()

    # initialise all velocity columns as NaN on every row
    for col in VELOCITY_COLUMNS:
        df[col] = float("nan")

    # apply velocity values only to the 11:30 row
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    midday_mask = (ts.dt.hour == _MIDDAY_HOUR) & (ts.dt.minute == _MIDDAY_MIN)

    for col, val in velocity.items():
        if col not in df.columns:
            continue
        # coerce integer velocity columns
        if col in VELOCITY_INTEGER_COLUMNS:
            try:
                int_val = int(round(val)) if pd.notna(val) and val == val else None
                df.loc[midday_mask, col] = int_val
            except (TypeError, ValueError):
                df.loc[midday_mask, col] = None
        else:
            df.loc[midday_mask, col] = val

    # bump schema_version on 11:30 row to signal enrichment
    if "schema_version" in df.columns:
        df.loc[midday_mask, "schema_version"] = schema_version

    return df


def _write_day_parquet(df: pd.DataFrame, output_root: Path, trade_date: str) -> None:
    """Write one day's enriched rows to year-partitioned parquet."""
    try:
        year = int(str(trade_date)[:4])
    except (ValueError, TypeError):
        year = 9999

    year_dir = output_root / f"year={year}"
    year_dir.mkdir(parents=True, exist_ok=True)

    # use trade_date as a unique filename to allow partial overwrites
    out_file = year_dir / f"{trade_date}.parquet"
    df.to_parquet(out_file, index=False, engine="pyarrow")


def _enumerate_source_dates(
    ml_flat_root: Path,
    start_date: str,
    end_date: str,
) -> List[str]:
    """Return all trade_dates in [start_date, end_date] that exist in ml_flat."""
    try:
        import duckdb
    except ImportError:
        return []

    glob_pattern = (ml_flat_root / "**" / "*.parquet").as_posix()
    try:
        con = duckdb.connect(":memory:")
        df: pd.DataFrame = con.execute(
            f"""
            SELECT DISTINCT trade_date
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date ASC
            """,
            [start_date, end_date],
        ).df()
        con.close()
    except Exception as exc:
        logger.error("date enumeration failed: %s", exc)
        return []

    return df["trade_date"].astype(str).tolist()


# ────────────────────────────────────────────────────────────────────────────────
# Per-date processing (designed to run in worker processes)
# ────────────────────────────────────────────────────────────────────────────────

def _process_one_date(
    trade_date: str,
    *,
    parquet_root: Path,
    output_root: Path,
    ml_flat_dataset: str,
    raw_dataset: str,
    all_dates_sorted: List[str],
    dry_run: bool,
    already_processed: Set[str],
    midday_option_volume_by_date: Optional[Dict[str, float]] = None,
) -> EnrichmentResult:
    """Process a single trade_date end-to-end.  Returns EnrichmentResult."""

    if trade_date in already_processed:
        return EnrichmentResult(trade_date=trade_date, status="skipped")

    ml_flat_root = parquet_root / ml_flat_dataset

    # ── 1. load full day ml_flat ───────────────────────────────────────────────
    full_day_df = _load_full_day_ml_flat(trade_date, ml_flat_root)
    if len(full_day_df) == 0:
        return EnrichmentResult(trade_date=trade_date, status="no_morning_data")

    # ── 2. find 11:30 midday row ───────────────────────────────────────────────
    midday_row = _find_midday_row(full_day_df)
    if midday_row is None:
        return EnrichmentResult(trade_date=trade_date, status="no_midday_row")

    # ── 3. load morning session (10:00–11:30) with IV enrichment ──────────────
    loader = MorningSessionLoader(
        parquet_root,
        ml_flat_dataset=ml_flat_dataset,
        raw_dataset=raw_dataset,
    )
    morning_df = loader.load(trade_date)

    if len(morning_df) == 0:
        return EnrichmentResult(trade_date=trade_date, status="no_morning_data")

    # ── 4. get previous day close (for gap features) ───────────────────────────
    prev_close = _get_prev_day_close(trade_date, all_dates_sorted, ml_flat_root)
    prev_day_midday_option_volume, avg_20d_midday_option_volume = _compute_volume_context(
        trade_date,
        all_dates_sorted,
        midday_option_volume_by_date or {},
    )

    # ── 5. compute velocity features ──────────────────────────────────────────
    try:
        velocity = compute_velocity_features(
            morning_df,
            midday_snapshot=midday_row,
            prev_day_close=prev_close,
            prev_day_midday_option_volume=prev_day_midday_option_volume,
            avg_20d_midday_option_volume=avg_20d_midday_option_volume,
        )
    except Exception as exc:
        logger.error("velocity computation failed for %s: %s", trade_date, exc)
        return EnrichmentResult(trade_date=trade_date, status="error", error=str(exc))

    # ── 6. attach velocity to full day df ──────────────────────────────────────
    enriched_df = _attach_velocity_to_day(full_day_df, velocity)

    # count NaN velocity cols on the 11:30 row for diagnostics
    ts = pd.to_datetime(enriched_df["timestamp"], errors="coerce")
    midday_mask = (ts.dt.hour == _MIDDAY_HOUR) & (ts.dt.minute == _MIDDAY_MIN)
    if midday_mask.any():
        midday_enriched = enriched_df[midday_mask].iloc[0]
        nan_count = sum(1 for c in VELOCITY_COLUMNS if pd.isna(midday_enriched.get(c)))
    else:
        nan_count = len(VELOCITY_COLUMNS)

    # ── 7. write to output (skip in dry-run) ───────────────────────────────────
    if not dry_run:
        try:
            _write_day_parquet(enriched_df, output_root, trade_date)
        except Exception as exc:
            logger.error("write failed for %s: %s", trade_date, exc)
            return EnrichmentResult(trade_date=trade_date, status="error", error=str(exc))

    return EnrichmentResult(
        trade_date=trade_date,
        status="enriched",
        rows_written=len(enriched_df),
        velocity_nan_count=nan_count,
    )


# ────────────────────────────────────────────────────────────────────────────────
# Main runner
# ────────────────────────────────────────────────────────────────────────────────

class EnrichmentBatchRunner:
    """
    Runs the enrichment backfill for a date range.

    Example:
        runner = EnrichmentBatchRunner(
            parquet_root="/path/to/parquet_data",
            start_date="2020-01-01",
            end_date="2024-12-31",
        )
        summary = runner.run()
    """

    def __init__(
        self,
        parquet_root: str | Path,
        start_date: str,
        end_date: str,
        *,
        output_dataset: str = DEFAULT_OUTPUT_DATASET,
        ml_flat_dataset: str = DEFAULT_ML_FLAT_DATASET,
        raw_dataset: str = DEFAULT_RAW_DATASET,
        dry_run: bool = False,
        workers: int = 1,
        resume: bool = True,
    ) -> None:
        self.parquet_root = Path(parquet_root)
        self.start_date = start_date
        self.end_date = end_date
        self.output_dataset = output_dataset
        self.ml_flat_dataset = ml_flat_dataset
        self.raw_dataset = raw_dataset
        self.dry_run = dry_run
        self.workers = max(1, int(workers))
        self.resume = resume

        self.output_root = self.parquet_root / self.output_dataset
        self.ml_flat_root = self.parquet_root / self.ml_flat_dataset

    def run(self) -> RunSummary:
        t_start = time.monotonic()
        summary = RunSummary()

        # ── enumerate source dates ─────────────────────────────────────────────
        logger.info(
            "Enumerating source dates %s – %s from %s",
            self.start_date, self.end_date, self.ml_flat_root,
        )
        all_dates = _enumerate_source_dates(self.ml_flat_root, self.start_date, self.end_date)
        if not all_dates:
            logger.warning("No dates found in source dataset for range %s – %s", self.start_date, self.end_date)
            return summary
        summary.total_dates = len(all_dates)
        logger.info("Found %d source dates", len(all_dates))
        midday_option_volume_by_date = _load_midday_option_volume_lookup(self.ml_flat_root)

        # ── resume: find already-processed dates ───────────────────────────────
        already_processed: Set[str] = set()
        if self.resume and not self.dry_run:
            already_processed = _get_already_processed_dates(self.output_root)
            if already_processed:
                logger.info("Resuming — skipping %d already-processed dates", len(already_processed))

        # ── build work list ────────────────────────────────────────────────────
        work_dates = [d for d in all_dates if d not in already_processed] if self.resume else all_dates
        summary.skipped = len(all_dates) - len(work_dates)

        if not work_dates:
            logger.info("All dates already processed — nothing to do")
            return summary

        logger.info(
            "Processing %d dates (%s dry_run=%s workers=%d)",
            len(work_dates), "DRY RUN" if self.dry_run else "WRITING", self.dry_run, self.workers,
        )

        # ── process dates ──────────────────────────────────────────────────────
        kwargs_common = dict(
            parquet_root=self.parquet_root,
            output_root=self.output_root,
            ml_flat_dataset=self.ml_flat_dataset,
            raw_dataset=self.raw_dataset,
            all_dates_sorted=all_dates,
            midday_option_volume_by_date=midday_option_volume_by_date,
            dry_run=self.dry_run,
            already_processed=already_processed,
        )

        if self.workers <= 1 or self.dry_run:
            results = [
                _process_one_date(d, **kwargs_common)  # type: ignore[arg-type]
                for d in work_dates
            ]
        else:
            with multiprocessing.Pool(processes=self.workers) as pool:
                tasks = [
                    pool.apply_async(_process_one_date, (d,), kwargs_common)
                    for d in work_dates
                ]
                results = [t.get() for t in tasks]

        # ── aggregate summary ──────────────────────────────────────────────────
        for i, res in enumerate(results):
            if res.status == "enriched":
                summary.enriched += 1
                summary.rows_written += res.rows_written
                if res.velocity_nan_count > 0:
                    logger.debug(
                        "%s enriched with %d NaN velocity cols",
                        res.trade_date, res.velocity_nan_count,
                    )
            elif res.status == "skipped":
                summary.skipped += 1
            elif res.status == "no_midday_row":
                summary.no_midday += 1
                logger.warning("%s: no 11:30 row found — skipped", res.trade_date)
            elif res.status == "no_morning_data":
                summary.no_morning_data += 1
                logger.debug("%s: no morning data — skipped", res.trade_date)
            elif res.status == "error":
                summary.errors += 1
                summary.failed_dates.append(res.trade_date)
                logger.error("%s: %s", res.trade_date, res.error)

            # progress log
            if (i + 1) % _LOG_EVERY_N_DATES == 0:
                logger.info(
                    "Progress: %d/%d  enriched=%d errors=%d",
                    i + 1, len(work_dates), summary.enriched, summary.errors,
                )

        summary.duration_seconds = time.monotonic() - t_start

        logger.info(
            "Enrichment complete: total=%d enriched=%d skipped=%d "
            "no_midday=%d no_morning=%d errors=%d rows_written=%d duration=%.1fs",
            summary.total_dates,
            summary.enriched,
            summary.skipped,
            summary.no_midday,
            summary.no_morning_data,
            summary.errors,
            summary.rows_written,
            summary.duration_seconds,
        )

        return summary


# ────────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ────────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backfill velocity features onto snapshots_ml_flat",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--parquet-root", required=True, help="Base path of parquet store")
    p.add_argument("--start-date", required=True, help="Start trade_date YYYY-MM-DD (inclusive)")
    p.add_argument("--end-date", required=True, help="End trade_date YYYY-MM-DD (inclusive)")
    p.add_argument(
        "--output-dataset",
        default=DEFAULT_OUTPUT_DATASET,
        help=f"Output dataset name under parquet-root (default: {DEFAULT_OUTPUT_DATASET})",
    )
    p.add_argument(
        "--ml-flat-dataset",
        default=DEFAULT_ML_FLAT_DATASET,
        help=f"Source ml_flat dataset name (default: {DEFAULT_ML_FLAT_DATASET})",
    )
    p.add_argument(
        "--raw-dataset",
        default=DEFAULT_RAW_DATASET,
        help=f"Raw snapshots dataset name for IV enrichment (default: {DEFAULT_RAW_DATASET})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate first 10 dates without writing output",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel worker processes (default: 1; use 4 for 4-core VM)",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Reprocess all dates even if already in output",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    parquet_root = Path(args.parquet_root)
    if not parquet_root.exists():
        logger.error("parquet-root does not exist: %s", parquet_root)
        return 1

    # dry-run: only process first 10 dates
    start = args.start_date
    end = args.end_date
    if args.dry_run:
        logger.info("DRY RUN mode — will process at most 10 dates without writing")

    runner = EnrichmentBatchRunner(
        parquet_root=parquet_root,
        start_date=start,
        end_date=end,
        output_dataset=args.output_dataset,
        ml_flat_dataset=args.ml_flat_dataset,
        raw_dataset=args.raw_dataset,
        dry_run=args.dry_run,
        workers=args.workers,
        resume=not args.no_resume,
    )

    # for dry-run, clamp to 10 dates by running against a shortened date range
    if args.dry_run:
        all_dates = _enumerate_source_dates(
            parquet_root / args.ml_flat_dataset, start, end
        )
        if all_dates:
            end = all_dates[min(9, len(all_dates) - 1)]
            runner.end_date = end
            logger.info("Dry-run clamped to dates up to %s", end)

    summary = runner.run()

    if summary.errors > 0:
        logger.warning(
            "%d dates failed. Re-run with --log-level DEBUG for details. "
            "Failed dates: %s",
            summary.errors,
            summary.failed_dates[:20],
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

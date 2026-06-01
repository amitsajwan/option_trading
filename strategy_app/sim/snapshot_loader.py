"""Parquet snapshot loader for the multi-day sim (MD-S2).

Public API
----------
load_day(trade_date, parquet_base) -> list[dict]
    Returns ordered intraday snapshot dicts for one trading day, in the format the
    strategy engine's evaluate() expects.  Returns [] for missing / holiday days.

available_days(parquet_base, date_from, date_to) -> list[str]
    Return sorted list of trading day strings with snapshot data in the range.

The loader reads from the canonical 'snapshots' parquet dataset
(.data/ml_pipeline/parquet_data/snapshots/). Each row has a `snapshot_raw_json`
column containing the full JSON snapshot dict. If `snapshot_raw_json` is absent
(older builds), the loader falls back to constructing a dict from the flat columns.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default parquet base — override with PARQUET_BASE env var or pass explicitly.
_DEFAULT_PARQUET_BASE = os.getenv(
    "PARQUET_BASE",
    "/app/.data/ml_pipeline/parquet_data",
)


def load_day(
    trade_date: str,
    parquet_base: Optional[str] = None,
) -> List[dict]:
    """Load ordered intraday snapshot dicts for a single trading day.

    Args:
        trade_date:   ISO date string "YYYY-MM-DD".
        parquet_base: Path to the parquet data root. Defaults to the PARQUET_BASE
                      env var or "/app/.data/ml_pipeline/parquet_data".

    Returns:
        Ordered list of snapshot dicts suitable for engine.evaluate(). Empty list
        when the day has no data (holiday, missing, before coverage starts).

    Raises:
        FileNotFoundError: if parquet_base does not exist (programming error).
        ImportError:       if duckdb is not installed.
    """
    base = Path(parquet_base or _DEFAULT_PARQUET_BASE)
    if not base.exists():
        raise FileNotFoundError(
            f"Parquet base path does not exist: {base}. "
            "Set PARQUET_BASE env var or pass parquet_base explicitly."
        )

    from snapshot_app.historical.parquet_store import ParquetStore

    store = ParquetStore(base)
    try:
        df = store.snapshots_for_date_range(trade_date, trade_date)
    finally:
        store.close()

    if df is None or len(df) == 0:
        logger.debug("snapshot_loader: no data for %s", trade_date)
        return []

    snapshots: List[dict] = []

    if "snapshot_raw_json" in df.columns:
        for raw in df["snapshot_raw_json"]:
            if raw is None:
                continue
            try:
                snap = json.loads(raw) if isinstance(raw, str) else dict(raw)
                snapshots.append(snap)
            except Exception:
                logger.warning("snapshot_loader: could not parse snapshot_raw_json row for %s", trade_date)
    else:
        # Fallback: build dict from flat columns. This covers parquet builds that
        # stored columns directly rather than serialising the full snapshot JSON.
        logger.info(
            "snapshot_loader: snapshot_raw_json not found in parquet — using flat columns for %s",
            trade_date,
        )
        records = df.to_dict(orient="records")
        for rec in records:
            # Normalise: convert NaN to None so downstream JSON-tolerant code handles it.
            snap = {k: (None if _is_nan(v) else v) for k, v in rec.items()}
            snapshots.append(snap)

    logger.info("snapshot_loader: loaded %d snapshots for %s", len(snapshots), trade_date)
    return snapshots


def available_days(
    parquet_base: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[str]:
    """Return sorted list of trade date strings that have snapshot data.

    Args:
        parquet_base: Path to parquet data root (defaults to PARQUET_BASE env var).
        date_from:    Optional "YYYY-MM-DD" lower bound (inclusive).
        date_to:      Optional "YYYY-MM-DD" upper bound (inclusive).

    Returns:
        Sorted list of "YYYY-MM-DD" strings. Empty if no data or path missing.
    """
    base = Path(parquet_base or _DEFAULT_PARQUET_BASE)
    if not base.exists():
        return []

    from snapshot_app.historical.parquet_store import ParquetStore

    store = ParquetStore(base)
    try:
        return store.available_snapshot_days(min_day=date_from, max_day=date_to)
    finally:
        store.close()


def _is_nan(v) -> bool:
    try:
        import math
        return isinstance(v, float) and math.isnan(v)
    except Exception:
        return False

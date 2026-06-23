"""Backfill enrichment features into phase1_market_snapshots.

When a new feature group is added to snapshot_app/core, run this script to
populate it across all stored snapshots so replay on old dates works correctly.

Usage (run inside the dashboard container):
  python3 /app/ops/gcp/backfill_snapshot_features.py
  python3 /app/ops/gcp/backfill_snapshot_features.py --group direction
  python3 /app/ops/gcp/backfill_snapshot_features.py --group all --dry-run
  python3 /app/ops/gcp/backfill_snapshot_features.py --group all --from 2026-06-01

Adding a new feature group:
  1. Add an entry to FEATURE_GROUPS below (sentinel col, depends_on, enrich fn)
  2. Write an _enrich_<name> function that takes a per-day sorted DataFrame
     and returns it with new columns added
  3. Run: python3 backfill_snapshot_features.py --group <name>

Column name mapping (IMPORTANT — do not skip):
  MongoDB stores raw OHLCV as fut_close/fut_high/fut_low/fut_open/fut_volume.
  Enrichment functions expect the canonical ML names: close/high/low/open/volume.
  The _load_day_df() function handles this remapping. If a new enrichment function
  needs additional fields, add the mapping in _COLUMN_REMAP below.
"""
from __future__ import annotations

import argparse
import math
import sys
from typing import Any, Dict, List, Optional

import pandas as pd
from pymongo import MongoClient, UpdateOne

MONGO_URI = "mongodb://mongo:27017"
DB_NAME = "trading_ai"
COLLECTION = "phase1_market_snapshots"

# ── Column remap: MongoDB field path → DataFrame column name used by enrichment fns ──
# futures_bar fields
_COLUMN_REMAP: Dict[str, str] = {
    "fut_close":  "close",
    "fut_high":   "high",
    "fut_low":    "low",
    "fut_open":   "open",
    "fut_volume": "volume",
    "fut_oi":     "oi",
}
# futures_derived and session_levels fields are kept as-is (ema_9, vwap, etc.)
# session_levels fields are remapped via _SESSION_LEVELS_REMAP
_SESSION_LEVELS_REMAP: Dict[str, str] = {
    "prev_day_high": "day_high",
    "prev_day_low":  "day_low",
    "prev_day_close": "day_close",
}


# ── Enrichment functions ───────────────────────────────────────────────────────

def _enrich_compression(df: pd.DataFrame) -> pd.DataFrame:
    from snapshot_app.core.compression_features import add_compression_features
    return add_compression_features(df)


def _enrich_direction(df: pd.DataFrame) -> pd.DataFrame:
    from snapshot_app.core.direction_features import add_direction_features
    return add_direction_features(df)


# ── Feature group registry ────────────────────────────────────────────────────
# sentinel: one column that must exist for the group to be considered "present"
# depends_on: list of group names that must be backfilled first
# enrich: function(df) -> df  (operates on a single day's sorted DataFrame)
# columns: set of columns written to payload.snapshot.futures_derived
FEATURE_GROUPS: Dict[str, Dict[str, Any]] = {
    "compression": {
        "sentinel": "compression_score",
        "depends_on": [],
        "enrich": _enrich_compression,
        "columns": None,  # populated at runtime from COMPRESSION_FEATURE_COLUMNS
    },
    "direction": {
        "sentinel": "dir_score",
        "depends_on": ["compression"],
        "enrich": _enrich_direction,
        "columns": None,  # populated at runtime from DIRECTION_FEATURE_COLUMNS
    },
}


def _resolve_columns() -> None:
    from snapshot_app.core.compression_features import COMPRESSION_FEATURE_COLUMNS
    from snapshot_app.core.direction_features import DIRECTION_FEATURE_COLUMNS
    FEATURE_GROUPS["compression"]["columns"] = list(COMPRESSION_FEATURE_COLUMNS)
    FEATURE_GROUPS["direction"]["columns"] = list(DIRECTION_FEATURE_COLUMNS)


def _load_day_df(coll: Any, trade_date: str) -> pd.DataFrame:
    """Load all snapshots for one trading day into a DataFrame with canonical column names."""
    docs = list(coll.find(
        {"trade_date_ist": trade_date},
        {
            "_id": 1,
            "payload.snapshot.futures_bar": 1,
            "payload.snapshot.futures_derived": 1,
            "payload.snapshot.session_context": 1,
            "payload.snapshot.session_levels": 1,
        },
        sort=[("payload.snapshot.session_context.timestamp", 1)],
    ))
    rows = []
    for doc in docs:
        snap = (doc.get("payload") or {}).get("snapshot") or {}
        fb = snap.get("futures_bar") or {}
        fd = snap.get("futures_derived") or {}
        sl = snap.get("session_levels") or {}

        row: Dict[str, Any] = {"_id": doc["_id"]}
        # Raw OHLCV — remapped to canonical ML names
        for src, dst in _COLUMN_REMAP.items():
            row[dst] = fb.get(src)
        # session_levels — remapped
        for src, dst in _SESSION_LEVELS_REMAP.items():
            row[dst] = sl.get(src)
        # futures_derived — kept as-is (ema_9, ema_21, ema_50, vwap, atr_ratio, etc.)
        for k, v in fd.items():
            if k not in row:  # don't overwrite remapped values
                row[k] = v
        rows.append(row)

    df = pd.DataFrame(rows)
    for col in [c for c in df.columns if c != "_id"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _missing_dates(coll: Any, group: str, date_from: Optional[str], date_to: Optional[str]) -> List[str]:
    """Return dates that are missing the sentinel column for this feature group."""
    sentinel = FEATURE_GROUPS[group]["sentinel"]
    sentinel_path = f"payload.snapshot.futures_derived.{sentinel}"
    all_dates = sorted(coll.distinct("trade_date_ist"))
    if date_from:
        all_dates = [d for d in all_dates if d >= date_from]
    if date_to:
        all_dates = [d for d in all_dates if d <= date_to]
    missing = []
    for d in all_dates:
        has = coll.find_one({"trade_date_ist": d, sentinel_path: {"$exists": True}}, {"_id": 1})
        if has is None:
            missing.append(d)
    return missing


def _backfill_group(
    coll: Any,
    group: str,
    dates: List[str],
    dry_run: bool,
) -> int:
    cfg = FEATURE_GROUPS[group]
    enrich_fn = cfg["enrich"]
    columns: List[str] = cfg["columns"]
    total = 0

    for trade_date in dates:
        df = _load_day_df(coll, trade_date)
        if df.empty:
            _log(f"  {trade_date}: no documents — skipped")
            continue

        df = enrich_fn(df)

        new_cols = [c for c in columns if c in df.columns]
        if not new_cols:
            _log(f"  {trade_date}: enrichment produced no matching columns — check function output")
            _log(f"    df cols: {[c for c in df.columns if c != '_id'][:20]}")
            continue

        nan_rows = df[new_cols].isna().all(axis=1).sum()
        non_nan_rows = len(df) - nan_rows

        if dry_run:
            sample = {k: round(float(v), 4) if pd.notna(v) and not math.isnan(float(v)) else None
                      for k, v in df[new_cols].iloc[-1].items()}
            _log(f"  {trade_date}: {len(df)} docs | {non_nan_rows} would have ≥1 feature set | sample last bar: {sample}")
            continue

        ops = []
        for _, row in df.iterrows():
            upd = {
                f"payload.snapshot.futures_derived.{col}": float(row[col])
                for col in new_cols
                if pd.notna(row.get(col))
            }
            if upd:
                ops.append(UpdateOne({"_id": row["_id"]}, {"$set": upd}))

        n = coll.bulk_write(ops, ordered=False).modified_count if ops else 0
        total += n
        _log(f"  {trade_date}: {n}/{len(df)} updated ({non_nan_rows} had ≥1 feature, {nan_rows} all-NaN warmup bars)")

    return total


def _resolve_run_order(groups: List[str]) -> List[str]:
    """Topological sort respecting depends_on."""
    ordered: List[str] = []
    visited: set = set()

    def visit(g: str) -> None:
        if g in visited:
            return
        visited.add(g)
        for dep in FEATURE_GROUPS[g]["depends_on"]:
            if dep not in groups:
                groups.append(dep)  # auto-add dependency
            visit(dep)
        ordered.append(g)

    for g in groups:
        visit(g)
    return ordered


def _log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill snapshot enrichment features")
    parser.add_argument("--group", default="all",
        help="Feature group to backfill: compression | direction | all (default: all)")
    parser.add_argument("--from", dest="date_from", default=None,
        help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--to", dest="date_to", default=None,
        help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--dry-run", action="store_true",
        help="Print what would be done without writing")
    parser.add_argument("--force", action="store_true",
        help="Backfill ALL dates, not just missing ones")
    args = parser.parse_args()

    _resolve_columns()

    coll = MongoClient(MONGO_URI)[DB_NAME][COLLECTION]
    total_docs = coll.count_documents({})
    _log(f"Connected. Collection has {total_docs} documents.")

    if args.group == "all":
        groups = list(FEATURE_GROUPS.keys())
    elif args.group in FEATURE_GROUPS:
        groups = [args.group]
    else:
        _log(f"Unknown group '{args.group}'. Available: {list(FEATURE_GROUPS.keys())}")
        sys.exit(1)

    run_order = _resolve_run_order(groups)
    _log(f"Run order: {' → '.join(run_order)}")
    if args.dry_run:
        _log("DRY RUN — no writes will occur")

    grand_total = 0
    for group in run_order:
        _log(f"\n[{group}] sentinel={FEATURE_GROUPS[group]['sentinel']}")
        if args.force:
            all_dates = sorted(coll.distinct("trade_date_ist"))
            if args.date_from:
                all_dates = [d for d in all_dates if d >= args.date_from]
            if args.date_to:
                all_dates = [d for d in all_dates if d <= args.date_to]
            dates = all_dates
            _log(f"  --force: processing all {len(dates)} dates")
        else:
            dates = _missing_dates(coll, group, args.date_from, args.date_to)
            _log(f"  Missing on {len(dates)} dates: {dates}")

        if not dates:
            _log("  Nothing to do.")
            continue

        n = _backfill_group(coll, group, dates, dry_run=args.dry_run)
        if not args.dry_run:
            _log(f"  [{group}] total updated: {n}")
        grand_total += n

    if not args.dry_run:
        _log(f"\nDone. Grand total documents updated: {grand_total}")

        # Verification
        _log("\nVerification:")
        for group in run_order:
            sentinel = FEATURE_GROUPS[group]["sentinel"]
            path = f"payload.snapshot.futures_derived.{sentinel}"
            n = coll.count_documents({path: {"$exists": True}})
            _log(f"  {group} ({sentinel}): {n}/{total_docs} docs have it")


if __name__ == "__main__":
    main()

"""CLI for historical Layer-2 snapshot batch build and validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from .parquet_store import ParquetStore
from .snapshot_batch import run_snapshot_batch

DEFAULT_PARQUET_BASE = Path(r"C:\code\market\ml_pipeline\artifacts\data\parquet_data")
DEFAULT_INSTRUMENT = "BANKNIFTY-I"


def _extract_live_fields(payload: dict[str, Any]) -> set[str]:
    """Extract flattened field names from live snapshot payload/envelope."""
    if not isinstance(payload, dict):
        return set()

    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
    if not isinstance(snapshot, dict):
        return set()

    fields: set[str] = set()
    for key, value in snapshot.items():
        if isinstance(value, dict):
            fields.update(value.keys())
        else:
            fields.add(key)
    return fields


def validate_output(parquet_base: Path, n_days: int = 5, live_snapshot_path: str | None = None) -> None:
    """Validate produced snapshots for row counts, null rates, and schema parity."""
    store = ParquetStore(parquet_base)
    snapshot_days = store.available_snapshot_days()
    if not snapshot_days:
        print("[validate] No snapshot days found. Build snapshots first.")
        return

    sample_days = snapshot_days[-max(1, int(n_days)) :]
    print(f"[validate] Checking {len(sample_days)} recent day(s): {sample_days}")

    print("\n[validate] Row counts per day")
    all_ok = True
    for day in sample_days:
        df = store.snapshots_for_date_range(day, day)
        rows = int(len(df))
        status = "OK" if 370 <= rows <= 380 else ("WARN" if 300 <= rows <= 400 else "ERROR")
        if status != "OK":
            all_ok = False
        print(f"  {day}: {rows} rows [{status}]")
    if all_ok:
        print("  All row counts within expected range (370-380)")

    df_range = store.snapshots_for_date_range(sample_days[0], sample_days[-1])
    if len(df_range) == 0:
        print("[validate] No rows in selected date range.")
        return

    print("\n[validate] Null rates for key fields")
    key_fields = [
        "snapshot_id",
        "timestamp",
        "session_phase",
        "days_to_expiry",
        "fut_close",
        "fut_volume",
        "realized_vol_30m",
        "vol_ratio",
        "orh",
        "orl",
        "vix_current",
        "vix_regime",
        "atm_strike",
        "pcr",
        "max_pain",
        "atm_ce_close",
        "atm_pe_close",
        "atm_ce_iv",
        "atm_pe_iv",
        "iv_skew",
        "iv_percentile",
        "iv_regime",
        "prev_day_high",
        "prev_day_close",
        "week_high",
    ]
    n_total = float(len(df_range))
    for field in key_fields:
        if field not in df_range.columns:
            print(f"  {field:<30} MISSING [ERROR]")
            continue
        null_pct = float(df_range[field].isna().sum()) / n_total * 100.0
        status = "OK" if null_pct == 0.0 else ("WARN" if null_pct < 20.0 else "ERROR")
        print(f"  {field:<30} {null_pct:6.2f}% [{status}]")

    print("\n[validate] IV percentile distribution")
    if "iv_percentile" not in df_range.columns:
        print("  iv_percentile missing")
    else:
        iv = pd.to_numeric(df_range["iv_percentile"], errors="coerce").dropna()
        if len(iv) == 0:
            print("  No iv_percentile values found yet")
        else:
            print(f"  Count   : {len(iv)} / {len(df_range)}")
            print(f"  Min     : {iv.min():.2f}")
            print(f"  Median  : {iv.median():.2f}")
            print(f"  Max     : {iv.max():.2f}")
            print(f"  <40%    : {(iv < 40).mean() * 100:.2f}%")
            print(f"  40-75%  : {((iv >= 40) & (iv <= 75)).mean() * 100:.2f}%")
            print(f"  >75%    : {(iv > 75).mean() * 100:.2f}%")

    if live_snapshot_path:
        print("\n[validate] Live schema comparison")
        try:
            live_path = Path(live_snapshot_path)
            first_line = live_path.read_text(encoding="utf-8").splitlines()[0]
            live_payload = json.loads(first_line)
            live_fields = _extract_live_fields(live_payload)
            hist_fields = set(df_range.columns)

            meta_fields = {"trade_date", "year", "instrument", "schema_version", "schema_name", "snapshot_raw_json"}
            only_live = sorted(list(live_fields - hist_fields))
            only_hist = sorted(list((hist_fields - live_fields) - meta_fields))

            if only_live:
                print(f"  Fields only in live ({len(only_live)}): {only_live[:20]}")
            if only_hist:
                print(f"  Fields only in historical ({len(only_hist)}): {only_hist[:20]}")
            if not only_live and not only_hist:
                print("  Field alignment is clean.")
        except Exception as exc:
            print(f"  Could not compare live snapshot schema: {exc}")

    print("\n[validate] Done.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build historical Layer-2 snapshots from parquet Layer-1 inputs.")
    parser.add_argument("--base", default=str(DEFAULT_PARQUET_BASE), help=f"Parquet base path (default: {DEFAULT_PARQUET_BASE})")
    parser.add_argument("--instrument", default=DEFAULT_INSTRUMENT, help=f"Snapshot instrument (default: {DEFAULT_INSTRUMENT})")
    parser.add_argument("--min-day", default=None, help="Start date inclusive (YYYY-MM-DD)")
    parser.add_argument("--max-day", default=None, help="End date inclusive (YYYY-MM-DD)")
    parser.add_argument("--lookback-days", type=int, default=30, help="Futures lookback trading days for rolling metrics")
    parser.add_argument("--no-resume", action="store_true", help="Rebuild days even if already in snapshots parquet")
    parser.add_argument("--dry-run", action="store_true", help="Plan only, do not write")
    parser.add_argument("--validate-only", action="store_true", help="Skip build and run validation only")
    parser.add_argument("--validate-days", type=int, default=0, help="Validate recent N snapshot days after build (or in validate-only mode)")
    parser.add_argument("--live-snapshot", default=None, help="Path to live snapshot JSONL for schema comparison")
    parser.add_argument("--log-every", type=int, default=10, help="Print progress every N days")
    args = parser.parse_args()

    base = Path(args.base)
    if not base.exists():
        print(f"ERROR: parquet base path not found: {base}")
        return 1

    if args.validate_only:
        validate_output(base, n_days=(args.validate_days or 5), live_snapshot_path=args.live_snapshot)
        return 0

    result = run_snapshot_batch(
        parquet_base=base,
        instrument=args.instrument,
        min_day=args.min_day,
        max_day=args.max_day,
        lookback_days=args.lookback_days,
        resume=(not args.no_resume),
        dry_run=args.dry_run,
        log_every=args.log_every,
    )
    print(json.dumps(result, indent=2, default=str))

    if args.validate_days > 0 and (not args.dry_run):
        validate_output(base, n_days=args.validate_days, live_snapshot_path=args.live_snapshot)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

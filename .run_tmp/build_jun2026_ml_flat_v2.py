"""
Step 1 (v2) of June-2026 forward check pipeline.
Reads mongoexport JSONL → proper snapshots_ml_flat parquet using
_flatten_snapshot + _project_rows_to_ml_flat from snapshot_batch.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, '/home/amits/bmm_run')
sys.path.insert(0, '/opt/option_trading')

from snapshot_app.historical.snapshot_batch import _flatten_snapshot, _project_rows_to_ml_flat
import pandas as pd

JSONL_PATH = "/home/amits/jun2026_snaps.json"
OUT_BASE = Path("/home/amits/parquet_data")
SKIP_DAYS = {"2026-06-03", "2026-06-09"}   # < 100 bars / bad data


def main():
    rows_by_day: dict[str, list] = {}
    n_ok = 0
    n_skip = 0

    print(f"Reading {JSONL_PATH} ...")
    with open(JSONL_PATH, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                n_skip += 1
                continue

            trade_date = str(doc.get("trade_date_ist") or "").strip()
            if not trade_date or trade_date in SKIP_DAYS:
                n_skip += 1
                continue

            payload = doc.get("payload")
            if not isinstance(payload, dict):
                n_skip += 1
                continue
            snapshot = payload.get("snapshot")
            if not isinstance(snapshot, dict):
                n_skip += 1
                continue

            year = int(trade_date[:4])
            try:
                row = _flatten_snapshot(snapshot, trade_date=trade_date, year=year)
                row["build_source"] = "mongo_export_jun2026"
                row["build_run_id"] = "jun2026_fwd_check"
            except Exception as e:
                print(f"  flatten error {trade_date}: {e}")
                n_skip += 1
                continue

            rows_by_day.setdefault(trade_date, []).append(row)
            n_ok += 1

    print(f"Parsed: {n_ok} rows, skipped: {n_skip}")
    for day, rows in sorted(rows_by_day.items()):
        print(f"  {day}: {len(rows)} raw bars")

    # Project each day's rows to proper ml_flat format (renames fut_* → px_fut_*, etc.)
    print("\nProjecting to ml_flat format...")
    all_projected = []
    for day in sorted(rows_by_day.keys()):
        day_rows = rows_by_day[day]
        try:
            projected = _project_rows_to_ml_flat(
                day_rows,
                build_source="mongo_export_jun2026",
                build_run_id="jun2026_fwd_check",
            )
            print(f"  {day}: {len(day_rows)} → {len(projected)} projected rows")
            all_projected.extend(projected)
        except Exception as e:
            print(f"  ERROR projecting {day}: {e}")
            import traceback; traceback.print_exc()

    if not all_projected:
        print("ERROR: no projected rows, aborting")
        return

    df = pd.DataFrame(all_projected)
    print(f"\nTotal projected: {len(df)} rows, {len(df.columns)} columns")

    # Verify key columns
    for col in ["px_fut_close", "px_fut_open", "px_fut_high", "px_fut_low",
                "opt_flow_ce_oi_total", "opt_flow_pe_oi_total", "trade_date", "timestamp"]:
        if col in df.columns:
            nn = df[col].notna().sum()
            print(f"  {col}: {nn}/{len(df)} non-null")
        else:
            print(f"  {col}: MISSING")

    # Write by year (sort by trade_date + timestamp)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "timestamp"]).reset_index(drop=True)

    year_val = 2026
    out_dir = OUT_BASE / "snapshots_ml_flat" / f"year={year_val}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "data.parquet"
    df.to_parquet(out_path, index=False, compression="snappy")
    print(f"\nWrote: {len(df)} rows → {out_path}")
    print("Done.")


if __name__ == "__main__":
    main()

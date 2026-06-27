"""
Step 1 of June-2026 forward check pipeline.
Reads mongoexport JSONL → writes snapshots_ml_flat parquet (year=2026).
Self-contained: no snapshot_app import needed.
"""
import json
import sys
from pathlib import Path
import pandas as pd

JSONL_PATH = "/home/amits/jun2026_snaps.json"
OUT_BASE = Path("/home/amits/parquet_data")
SKIP_DAYS = {"2026-06-03", "2026-06-09"}   # < 100 bars / bad data

# Same block list as snapshot_app.historical.snapshot_batch._flatten_snapshot
_BLOCKS = (
    "session_context", "futures_bar", "futures_derived", "mtf_derived",
    "opening_range", "vix_context", "chain_aggregates", "ladder_aggregates",
    "atm_options", "iv_derived", "option_price", "session_levels",
)


def flatten_snapshot(snapshot: dict, trade_date: str, year: int) -> dict:
    row = {
        "trade_date": trade_date,
        "year": year,
        "instrument": snapshot.get("instrument"),
        "schema_version": snapshot.get("schema_version"),
        "schema_name": snapshot.get("schema_name", "MarketSnapshot"),
        "snapshot_id": snapshot.get("snapshot_id"),
    }
    for block_name in _BLOCKS:
        block = snapshot.get(block_name)
        if isinstance(block, dict):
            for key, value in block.items():
                row[key] = value
    return row


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
                print(f"  JSON parse error: {e}")
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
                row = flatten_snapshot(snapshot, trade_date=trade_date, year=year)
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
        print(f"  {day}: {len(rows)} bars")

    # Write by year
    all_rows = [r for rows in rows_by_day.values() for r in rows]
    df = pd.DataFrame(all_rows)
    df["trade_date"] = df["trade_date"].astype(str)
    print(f"Total DataFrame: {len(df)} rows, {len(df.columns)} columns")

    # Sample key columns
    for col in ["timestamp", "px_fut_close", "px_fut_open", "px_fut_high", "px_fut_low",
                "atm_ce_iv", "atm_pe_iv", "opt_flow_rows"]:
        if col in df.columns:
            nn = df[col].notna().sum()
            print(f"  {col}: {nn}/{len(df)} non-null")
        else:
            print(f"  {col}: MISSING")

    for year in df["year"].unique():
        year_df = df[df["year"] == int(year)].copy()
        year_df = year_df.sort_values(["trade_date", "timestamp"]).reset_index(drop=True)
        out_dir = OUT_BASE / "snapshots_ml_flat" / f"year={int(year)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "data.parquet"
        year_df.to_parquet(out_path, index=False, compression="snappy")
        print(f"Wrote year={year}: {len(year_df)} rows → {out_path}")

    print("Done.")


if __name__ == "__main__":
    main()

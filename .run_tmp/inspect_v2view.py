"""Inspect the training stage1_entry_view_v2 dataset: rows/day, null patterns for
velocity vs compression vs base columns. Ground truth to replicate for June 2026."""
import glob
import os

import duckdb

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, "parquet_data", "stage1_entry_view_v2")
g = os.path.join(ROOT, "**", "*.parquet")
files = glob.glob(g, recursive=True)
print("dataset_root=", ROOT, "files=", len(files))
if not files:
    raise SystemExit("no parquet in stage1_entry_view_v2")

con = duckdb.connect(":memory:")
rp = f"read_parquet('{g}', hive_partitioning=false, union_by_name=true)"

# total rows + day count + range
r = con.execute(f"SELECT COUNT(*) n, COUNT(DISTINCT trade_date) d, MIN(trade_date), MAX(trade_date) FROM {rp}").fetchall()
print("total_rows/days/range=", r)

# columns
cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {rp} LIMIT 1").fetchall()]
print("n_cols=", len(cols))

probe = [c for c in ["vel_price_delta_open", "vel_pcr_delta_open", "ctx_am_trend",
                     "bb_width_20", "compression_score", "range_ratio_10_30",
                     "fut_return_5m", "atr_ratio", "px_fut_close"] if c in cols]

# per-day non-null counts for a sample of 3 days
days = [x[0] for x in con.execute(f"SELECT DISTINCT trade_date FROM {rp} ORDER BY trade_date LIMIT 3").fetchall()]
for d in days:
    parts = [f"day={d}"]
    nrow = con.execute(f"SELECT COUNT(*) FROM {rp} WHERE trade_date='{d}'").fetchone()[0]
    parts.append(f"rows={nrow}")
    for c in probe:
        nn = con.execute(f"SELECT COUNT(*) FROM {rp} WHERE trade_date='{d}' AND \"{c}\" IS NOT NULL").fetchone()[0]
        parts.append(f"{c}={nn}")
    print("  ".join(parts))

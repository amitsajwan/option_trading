"""Dump columns of stage1_entry_view_v2 and snapshots_ml_flat_v2 (for label/key wiring)."""
import glob
import os

import duckdb

HOME = os.path.expanduser("~")
con = duckdb.connect(":memory:")


def cols(ds):
    g = os.path.join(HOME, "parquet_data", ds, "**", "*.parquet")
    files = glob.glob(g, recursive=True)
    if not files:
        print(f"\n## {ds}: NO FILES")
        return
    rp = f"read_parquet('{g}', hive_partitioning=false, union_by_name=true)"
    cs = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {rp} LIMIT 1").fetchall()]
    print(f"\n## {ds}  n_cols={len(cs)}  files={len(files)}")
    print(",".join(cs))
    # key + ohlc presence
    for k in ["trade_date", "timestamp", "snapshot_id", "px_fut_open", "px_fut_high",
              "px_fut_low", "px_fut_close"]:
        print(f"   has {k}: {k in cs}")


cols("stage1_entry_view_v2")
cols("snapshots_ml_flat_v2")

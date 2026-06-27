import subprocess, os, glob

# Check parquet data dirs on runtime VM
paths = [
    "/opt/option_trading/.data/ml_pipeline/parquet_data",
    "/home/amits/parquet_data",
    "/data/parquet_data",
]
for p in paths:
    if os.path.exists(p):
        print(f"EXISTS: {p}")
        try:
            print("  contents:", os.listdir(p))
        except:
            pass
    else:
        print(f"MISSING: {p}")

# Check for recent snapshots parquet
import duckdb
for p in paths:
    for ds in ["snapshots", "snapshots_ml_flat_v2", "snapshots_ml_flat"]:
        g = f"{p}/{ds}/**/*.parquet"
        files = glob.glob(g, recursive=True)
        if files:
            try:
                con = duckdb.connect(":memory:")
                r = con.execute(
                    f"SELECT min(trade_date) as lo, max(trade_date) as hi, count(*) as n "
                    f"FROM read_parquet('{g}', hive_partitioning=false, union_by_name=true)"
                ).df()
                print(f"  {ds}: {r.iloc[0].to_dict()}")
                con.close()
            except Exception as e:
                print(f"  {ds}: {e}")

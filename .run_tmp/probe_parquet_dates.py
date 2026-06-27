import duckdb, os, glob

base = "/home/amits/parquet_data"
print("Datasets:", os.listdir(base))

for ds in ["snapshots_ml_flat_v2", "stage1_entry_view_v2", "snapshots_ml_flat"]:
    path = f"{base}/{ds}/**/*.parquet"
    files = glob.glob(path, recursive=True)
    if not files:
        print(f"{ds}: NO FILES")
        continue
    try:
        con = duckdb.connect(":memory:")
        r = con.execute(
            f"SELECT min(trade_date) as lo, max(trade_date) as hi, count(*) as n "
            f"FROM read_parquet('{path}', hive_partitioning=false, union_by_name=true)"
        ).df()
        print(f"{ds}: {r.iloc[0].to_dict()}")
        con.close()
    except Exception as e:
        print(f"{ds}: ERROR {e}")

"""
query_test.py
─────────────────────────────────────────────────────────────────
Quick validation queries after convert_to_parquet.py runs.
Shows row counts, date ranges, and sample data for each dataset.

Usage:
    python query_test.py
    python query_test.py --out C:/code/market/ml_pipeline/artifacts/data/parquet_data
"""

import argparse
from pathlib import Path

import duckdb

DEFAULT_OUT = Path(r"C:\code\market\ml_pipeline\artifacts\data\parquet_data")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    out = Path(args.out)

    con = duckdb.connect()

    def q(label, sql):
        print(f"\n── {label} {'─' * (50 - len(label))}")
        try:
            result = con.execute(sql).df()
            print(result.to_string(index=False))
        except Exception as e:
            print(f"  ERROR: {e}")

    base = str(out).replace("\\", "/")

    # ── FUTURES ──────────────────────────────────────────────────────────────
    q("FUTURES — row count by year",
      f"SELECT year, COUNT(*) as rows FROM read_parquet('{base}/futures/**/*.parquet', hive_partitioning=true) GROUP BY year ORDER BY year")

    q("FUTURES — date range",
      f"SELECT MIN(trade_date) as first_day, MAX(trade_date) as last_day, COUNT(DISTINCT trade_date) as trading_days FROM read_parquet('{base}/futures/**/*.parquet', hive_partitioning=true)")

    q("FUTURES — sample (first 3 rows)",
      f"SELECT timestamp, symbol, open, high, low, close, volume, oi FROM read_parquet('{base}/futures/**/*.parquet', hive_partitioning=true) ORDER BY timestamp LIMIT 3")

    q("FUTURES — sample (last 3 rows)",
      f"SELECT timestamp, symbol, open, high, low, close, volume, oi FROM read_parquet('{base}/futures/**/*.parquet', hive_partitioning=true) ORDER BY timestamp DESC LIMIT 3")

    # ── OPTIONS ──────────────────────────────────────────────────────────────
    q("OPTIONS — row count by year",
      f"SELECT year, COUNT(*) as rows, COUNT(DISTINCT symbol) as unique_symbols FROM read_parquet('{base}/options/**/*.parquet', hive_partitioning=true) GROUP BY year ORDER BY year")

    q("OPTIONS — unique strikes sample",
      f"SELECT DISTINCT strike, option_type FROM read_parquet('{base}/options/**/*.parquet', hive_partitioning=true) WHERE trade_date = (SELECT MIN(trade_date) FROM read_parquet('{base}/options/**/*.parquet', hive_partitioning=true)) ORDER BY strike LIMIT 10")

    q("OPTIONS — ATM sample (one timestamp)",
      f"""
      WITH first_ts AS (
          SELECT MIN(timestamp) as ts FROM read_parquet('{base}/options/**/*.parquet', hive_partitioning=true)
      )
      SELECT symbol, strike, option_type, open, high, low, close, volume, oi
      FROM read_parquet('{base}/options/**/*.parquet', hive_partitioning=true)
      WHERE timestamp = (SELECT ts FROM first_ts)
      ORDER BY strike
      LIMIT 10
      """)

    # ── SPOT ─────────────────────────────────────────────────────────────────
    q("SPOT — row count by year",
      f"SELECT year, COUNT(*) as rows FROM read_parquet('{base}/spot/**/*.parquet', hive_partitioning=true) GROUP BY year ORDER BY year")

    q("SPOT — sample",
      f"SELECT timestamp, symbol, open, high, low, close FROM read_parquet('{base}/spot/**/*.parquet', hive_partitioning=true) ORDER BY timestamp LIMIT 3")

    # ── VIX ──────────────────────────────────────────────────────────────────
    q("VIX — full range",
      f"SELECT COUNT(*) as rows, MIN(trade_date) as first, MAX(trade_date) as last FROM read_parquet('{base}/vix/vix.parquet')")

    q("VIX — sample",
      f"SELECT trade_date, vix_open, vix_high, vix_low, vix_close, vix_prev_close FROM read_parquet('{base}/vix/vix.parquet') ORDER BY trade_date LIMIT 5")

    # ── CROSS INSTRUMENT CHECK ────────────────────────────────────────────────
    q("CROSS CHECK — futures vs options alignment (rows per day, first 5 days)",
      f"""
      SELECT
          f.trade_date,
          f.fut_rows,
          o.opt_rows
      FROM (
          SELECT trade_date, COUNT(*) as fut_rows
          FROM read_parquet('{base}/futures/**/*.parquet', hive_partitioning=true)
          GROUP BY trade_date
      ) f
      JOIN (
          SELECT trade_date, COUNT(*) as opt_rows
          FROM read_parquet('{base}/options/**/*.parquet', hive_partitioning=true)
          GROUP BY trade_date
      ) o ON f.trade_date = o.trade_date
      ORDER BY f.trade_date
      LIMIT 5
      """)

    print("\n── DONE ─────────────────────────────────────────────────────")
    print("If all row counts look correct, your Parquet data is ready.")
    print("Next: run the MSS snapshot builder against this data.")

    con.close()


if __name__ == "__main__":
    main()

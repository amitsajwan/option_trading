# Historical Snapshot User Guide

Builds Layer-2 historical `MarketSnapshot` parquet (MSS.1-MSS.9) from Layer-1 parquet inputs.

## What This Produces

Input (already converted):
- `ml_pipeline/artifacts/data/parquet_data/futures/year=YYYY/data.parquet`
- `ml_pipeline/artifacts/data/parquet_data/options/year=YYYY/data.parquet`
- `ml_pipeline/artifacts/data/parquet_data/spot/year=YYYY/data.parquet`
- `ml_pipeline/artifacts/data/parquet_data/vix/vix.parquet`

Output:
- `ml_pipeline/artifacts/data/parquet_data/snapshots/year=YYYY/data.parquet`

Each output row is one minute snapshot with flattened MSS fields plus:
- `trade_date`
- `year`
- `instrument`
- `schema_version`
- `snapshot_raw_json` (full nested snapshot)

## Prerequisites

Install deps (in your active environment):

```powershell
pip install -r snapshot_app/requirements.txt
```

If needed explicitly:

```powershell
pip install pandas pyarrow duckdb
```

## Verify Layer-1 Parquet First

From `ml_pipeline`:

```powershell
$env:PYTHONIOENCODING='utf-8'; python query_test.py
```

Note: use `python query_test.py`, not `run query_test.py`.

## Run Commands

From repo root (`C:\code\market`):

1. Dry run:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --min-day 2020-01-01 --max-day 2020-01-10 --dry-run
```

2. Small build slice:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --min-day 2020-01-01 --max-day 2020-01-05
```

3. Validate built data:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --validate-only --validate-days 5
```

4. Full build (resumable):

```powershell
python -m snapshot_app.historical.snapshot_batch_runner
```

## Broadcast Historical Snapshots (L3 Replay)

Replay prebuilt snapshots to Redis so downstream consumers (for example `strategy_app`) can consume historical data through the same event contract as live.

Default replay topic is `market:snapshot:v1:historical`.

```powershell
python -m snapshot_app.historical.replay_runner --start-date 2020-01-01 --end-date 2020-01-10 --speed 0
```

Real-time style speed (`1x`):

```powershell
python -m snapshot_app.historical.replay_runner --start-date 2020-01-01 --end-date 2020-01-01 --speed 1
```

Replay and stop after fixed number of events:

```powershell
python -m snapshot_app.historical.replay_runner --start-date 2020-01-01 --end-date 2020-01-10 --max-events 1000
```

## Resume and Rebuild Behavior

- Default mode is resumable: already-built days are skipped.
- If interrupted, run the same command again.
- To force rebuild a range:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --min-day 2020-01-01 --max-day 2020-01-05 --no-resume
```

## Interpreting Validation Output

- Row counts around `375` per day are expected.
- `prev_day_*` / `week_*` can be null on earliest day(s) because no prior session exists yet.
- `atm_ce_iv` or `atm_pe_iv` can be null on some rows when IV inversion has no valid mathematical solution (for example option price below intrinsic).
- `iv_skew` is null when either CE or PE IV is null.

## Important Operational Note

If you change historical code while batch is running:
1. Stop the running process.
2. Restart the runner so new code is loaded.

Running process check (PowerShell):

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'snapshot_app\\.historical\\.snapshot_batch_runner' } | Select-Object ProcessId,CommandLine
```

## Quick Health Check

```powershell
python -c "from snapshot_app.historical.parquet_store import ParquetStore; s=ParquetStore(r'C:\\code\\market\\ml_pipeline\\artifacts\\data\\parquet_data'); d=s.available_snapshot_days(); print(len(d), d[0] if d else None, d[-1] if d else None)"
```

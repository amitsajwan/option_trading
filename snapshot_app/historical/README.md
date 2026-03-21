# Historical Snapshot User Guide

Builds the final historical `MarketSnapshot` contract through one staged pipeline:

1. raw CSV input under a source root such as `C:\code\banknifty_data`
2. normalized parquet cache under `.data/ml_pipeline/parquet_data`
3. canonical `snapshots` parquet under the same parquet base
4. intermediate `market_base` parquet under the same parquet base
5. derived `snapshots_ml_flat` and stage-view parquet for ML research

The preferred operator entrypoint is always:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner
```

Internally, that runner now uses `snapshot_app.pipeline` for raw normalization and orchestration, so there is no separate "old historical builder" to keep in sync.

For the GCP operator flow that builds final parquet on a high-power VM and uploads it to GCS, see [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md).

## What This Produces

Input options:
- raw CSV root:
  - `C:\code\banknifty_data\banknifty_fut\YYYY\M\*.csv`
  - `C:\code\banknifty_data\banknifty_options\YYYY\M\*.csv`
  - `C:\code\banknifty_data\banknifty_spot\YYYY\M\*.csv`
  - `C:\code\banknifty_data\VIX\*.csv`
- or already-normalized parquet:
- `.data/ml_pipeline/parquet_data/futures/year=YYYY/month=MM/data.parquet`
- `.data/ml_pipeline/parquet_data/options/year=YYYY/month=MM/data.parquet`
- `.data/ml_pipeline/parquet_data/spot/year=YYYY/month=MM/data.parquet`
- `.data/ml_pipeline/parquet_data/vix/vix.parquet`

Output:
- `.data/ml_pipeline/parquet_data/snapshots/**/data.parquet` (canonical `MarketSnapshot` contract)
- `.data/ml_pipeline/parquet_data/market_base/**/data.parquet` (stateful flattened intermediate for downstream projection)
- `.data/ml_pipeline/parquet_data/snapshots_ml_flat/**/data.parquet` (derived ML-flat contract)
- `.data/ml_pipeline/parquet_data/stage1_entry_view/**/data.parquet`
- `.data/ml_pipeline/parquet_data/stage2_direction_view/**/data.parquet`
- `.data/ml_pipeline/parquet_data/stage3_recipe_view/**/data.parquet`

Each trading minute now produces:
- one canonical nested `MarketSnapshot`
- one `market_base` row for downstream projection
- one derived `snapshots_ml_flat` row for offline ML

Contract baseline:
- `schema_name = MarketSnapshot`
- `schema_version = 3.0`
- `chain_aggregates.strike_count` is required for rebuild gating
- `atm_ce_open/high/low` and `atm_pe_open/high/low` are strict nullable feed values (no fallback to close)

## Prerequisites

Install deps (in your active environment):

```powershell
pip install -r snapshot_app/requirements.txt
```

If needed explicitly:

```powershell
pip install pandas pyarrow duckdb
```

## Preferred Build Modes

### Mode A: Raw CSV to snapshots

This is the preferred mode when rebuilding from the full raw archive.

1. Normalize only:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --raw-root C:\code\banknifty_data --normalize-only
```

2. Normalize and build a small slice:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --raw-root C:\code\banknifty_data --min-day 2020-01-01 --max-day 2020-01-10
```

3. Full resumable raw-to-snapshot build:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --raw-root C:\code\banknifty_data
```

### Mode B: Build from existing parquet cache

This mode is still supported, but it uses the same runner and the same snapshot build path.

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --min-day 2020-01-01 --max-day 2020-01-10
```

## Verify Layer-1 Parquet First

From the repo root, after the parquet cache is present under `.data/ml_pipeline/parquet_data`:

```powershell
$env:PYTHONIOENCODING='utf-8'; python query_test.py
```

Note: use `python query_test.py`, not `run query_test.py`.

## Run Commands

From repo root (`C:\code\option_trading`):

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

5. Build canonical snapshots plus market_base only:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --build-stage snapshots
```

6. Build derived ML-flat plus stage views from existing market_base:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --build-stage derived --validate-ml-flat-contract
```

7. Build the full staged pipeline in one command:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --build-stage all --build-source historical --validate-ml-flat-contract --manifest-out .run/snapshot_ml_flat/team_b/build_manifest.json
```

8. Validate canonical snapshots plus derived flat dataset and write report:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --validate-only --validate-days 5 --validation-report-out .run/snapshot_ml_flat/team_b/validation_report.json
```

9. Print year-by-year parallel commands for a large range:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --build-stage all --min-day 2022-01-01 --max-day 2024-12-31 --validate-ml-flat-contract --validate-days 5 --manifest-out .run/snapshot_ml_flat/team_b/build_manifest.json --validation-report-out .run/snapshot_ml_flat/team_b/validation_report.json --plan-year-runs
```

10. Run one specific calendar year:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --build-stage all --year 2024 --validate-ml-flat-contract --validate-days 5
```

On larger machines, the runner now uses chunked calendar partitions with warmup continuity when `--snapshot-jobs > 1`.

Current performance levers:
- `--normalize-jobs` for raw CSV to parquet conversion
- `--snapshot-jobs` for parallel snapshot workers
- `--slice-months` for calendar months per parallel chunk
- `--slice-warmup-days` for pre-chunk state warmup

Recommended fast full-build settings:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --raw-root C:\code\banknifty_data --normalize-jobs 8 --snapshot-jobs 8 --slice-months 6 --slice-warmup-days 90
```

Important:
- the chunked builder writes under `year=YYYY/chunk=.../data.parquet`
- if you already have old legacy outputs like `snapshots/year=2024/data.parquet`, delete the existing snapshot output roots before the first full chunked rebuild

Legacy yearly/flat outputs to remove before a clean full rebuild:

```powershell
Remove-Item -Recurse -Force .data\ml_pipeline\parquet_data\snapshots
Remove-Item -Recurse -Force .data\ml_pipeline\parquet_data\snapshots_ml_flat
Remove-Item -Recurse -Force .data\ml_pipeline\parquet_data\stage1_entry_view
Remove-Item -Recurse -Force .data\ml_pipeline\parquet_data\stage2_direction_view
Remove-Item -Recurse -Force .data\ml_pipeline\parquet_data\stage3_recipe_view
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

## Replay From Mongo By Date

Replay one trading day from persisted Mongo snapshots (instead of parquet snapshots).

```powershell
python -m snapshot_app.historical.mongo_replay_runner --date 2026-03-06
```

For host setups where compose Mongo is exposed on `27019`:

```powershell
python -m snapshot_app.historical.mongo_replay_runner --date 2026-03-06 --mongo-port 27019
```

Dry run validation (no publish):

```powershell
python -m snapshot_app.historical.mongo_replay_runner --date 2026-03-06 --mongo-port 27019 --dry-run
```

Run a specific replay mode:

```powershell
python -m snapshot_app.historical.mongo_replay_runner --date 2026-03-06 --mongo-port 27019 --mode base_only
```

Run a multi-mode matrix on one date:

```powershell
python -m snapshot_app.historical.mongo_replay_runner --date 2026-03-06 --mongo-port 27019 --matrix
```

Supported modes:
- `current`: current runtime behavior
- `base_only`: force deterministic/base policy (disable ML wrapper)
- `no_iv_filter`: remove `IV_FILTER` from entry path
- `base_no_iv_filter`: base-only + no `IV_FILTER`
- `ml_score_all`: keep runtime behavior and also shadow-score every snapshot through ML (diagnostic only)

Notes:
- default topic is `market:snapshot:v1:historical`
- command injects `metadata.run_id` and replay markers for run-scoped analysis
- command prints replay result JSON and historical summary JSON
- live topic publish is blocked unless `--allow-live-topic` is provided explicitly

## Resume and Rebuild Behavior

- Default mode is resumable: already-built days are skipped.
- If interrupted, run the same command again.
- To force rebuild a range:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --min-day 2020-01-01 --max-day 2020-01-05 --no-resume
```

- To rebuild only days missing specific fields:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --rebuild-missing-fields --required-fields vwap ema_9 ema_21 ema_50
```

This is the safe way to backfill newly added snapshot features into already-created parquet days.

## Interpreting Validation Output

- Row counts around `375` per day are expected.
- `prev_day_*` / `week_*` can be null on earliest day(s) because no prior session exists yet.
- `atm_ce_iv` or `atm_pe_iv` can be null on some rows when IV inversion has no valid mathematical solution (for example option price below intrinsic).
- `iv_skew` is null when either CE or PE IV is null.

## Important Operational Note

If you change historical code while batch is running:
1. Stop the running process.
2. Restart the runner so new code is loaded.

Important:
- days already written before the restart keep the old schema/features until you rebuild them
- use `--rebuild-missing-fields` after the code change to patch only incomplete days in place

Running process check (PowerShell):

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'snapshot_app\\.historical\\.snapshot_batch_runner' } | Select-Object ProcessId,CommandLine
```

## Quick Health Check

```powershell
python -c "from snapshot_app.historical.parquet_store import ParquetStore; s=ParquetStore(r'C:\\code\\option_trading\\.data\\ml_pipeline\\parquet_data'); d=s.available_snapshot_days(); print(len(d), d[0] if d else None, d[-1] if d else None)"
```

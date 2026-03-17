# GCP Snapshot Parquet Run Guide

This is the operator path for rebuilding the final historical parquet datasets on a high-power GCP machine and saving them to shared Cloud Storage.

Use this when you want:

- canonical historical `MarketSnapshot` parquet
- derived `snapshots_ml_flat` parquet for ML research
- a resumable build path from raw BankNifty archive
- outputs stored in GCS so they are accessible from other VMs and future runs

If you do not have base GCP infra or shared snapshot storage yet, start with [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md).

## 1. What This Produces

The snapshot pipeline writes two final datasets under `.data/ml_pipeline/parquet_data`:

- `snapshots/year=YYYY/data.parquet`
- `snapshots_ml_flat/year=YYYY/data.parquet`

The recommended GCS target is:

- raw archive prefix:
  - `gs://<snapshot-data-bucket>/banknifty_data`
- final parquet prefix:
  - `gs://<snapshot-data-bucket>/parquet_data`

## 2. Recommended Machine

Do the full rebuild on a disposable Linux VM with enough CPU and disk.

Recommended starting point:

- `n2-highmem-32`
- at least `500 GB` balanced persistent disk

The default training template is still smaller. For a full multi-year historical rebuild, temporarily use a larger machine for the snapshot build window, then delete it after upload.

## 3. One-Time Storage Setup

In [operator.env.example](../ops/gcp/operator.env.example), fill these optional snapshot-storage values in your real `ops/gcp/operator.env`:

```bash
SNAPSHOT_DATA_BUCKET_NAME="my-option-trading-snapshots"
RAW_ARCHIVE_BUCKET_URL="gs://my-option-trading-snapshots/banknifty_data"
SNAPSHOT_PARQUET_BUCKET_URL="gs://my-option-trading-snapshots/parquet_data"
```

If `SNAPSHOT_DATA_BUCKET_NAME` is set before running [from_scratch_bootstrap.sh](../ops/gcp/from_scratch_bootstrap.sh), Terraform will create the snapshot-data bucket too.

## 4. Upload Raw Archive Once

From a machine that has the raw archive locally:

```bash
export RAW_ARCHIVE_BUCKET_URL="gs://my-option-trading-snapshots/banknifty_data"
export RAW_DATA_ROOT="/path/to/banknifty_data"
./ops/gcp/publish_raw_market_data.sh
```

Expected raw root layout:

- `banknifty_fut`
- `banknifty_options`
- `banknifty_spot`
- `VIX`

## 5. Build And Publish Final Parquet

On the high-power GCP VM:

```bash
cd ~/option_trading
git pull
```

Then run:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=24
export SNAPSHOT_JOBS=8
export VALIDATE_DAYS=5
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

What this does:

1. syncs raw archive from `RAW_ARCHIVE_BUCKET_URL` to local disk
2. creates or reuses `.venv`
3. installs `snapshot_app` build dependencies
4. runs `snapshot_app.historical.snapshot_batch_runner`
5. writes:
   - build manifest
   - validation report
   - latest window manifest
6. uploads final parquet and reports to `SNAPSHOT_PARQUET_BUCKET_URL`

## 6. Useful Variants

Build one year only:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export YEAR=2024
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Build a small date slice:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export MIN_DAY=2024-01-01
export MAX_DAY=2024-03-31
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Validate only against already-built local parquet:

```bash
export VALIDATE_ONLY=1
export PUBLISH_SNAPSHOT_PARQUET=0
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Rebuild a range from scratch:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NO_RESUME=1
export YEAR=2024
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Also upload normalized parquet cache:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export PUBLISH_NORMALIZED_CACHE=1
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

## 7. Published Layout In GCS

After a successful upload, the bucket prefix should contain:

- `parquet_data/snapshots/year=YYYY/data.parquet`
- `parquet_data/snapshots_ml_flat/year=YYYY/data.parquet`
- `parquet_data/reports/<build_run_id>/build_manifest.json`
- `parquet_data/reports/<build_run_id>/validation_report.json`
- `parquet_data/reports/<build_run_id>/window_manifest_latest.json`

If `PUBLISH_NORMALIZED_CACHE=1`, it will also contain:

- `parquet_data/normalized/futures/...`
- `parquet_data/normalized/options/...`
- `parquet_data/normalized/spot/...`
- `parquet_data/normalized/vix/...`

## 8. Resume Strategy

The historical runner is resumable by default.

That means you can:

- rerun the same command after interruption
- keep the local parquet base on the VM disk during the build window
- republish to the same GCS prefix after more years or more days are completed

The easiest steady-state pattern is:

1. keep raw archive in GCS
2. rebuild on a disposable high-power VM
3. upload final parquet to `SNAPSHOT_PARQUET_BUCKET_URL`
4. delete the VM after success

## 9. Verification Commands

List generated local datasets:

```bash
find .data/ml_pipeline/parquet_data -maxdepth 2 -type d
```

Validate recent days:

```bash
python -m snapshot_app.historical.snapshot_batch_runner --validate-only --validate-days 5
```

Inspect the uploaded GCS layout:

```bash
gcloud storage ls "gs://my-option-trading-snapshots/parquet_data/**"
```

## 10. Related Files

- [ops/gcp/README.md](../ops/gcp/README.md)
- [snapshot_app/historical/README.md](../snapshot_app/historical/README.md)
- [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md)
- [FROM_SCRATCH_OPERATOR_GUIDE.md](FROM_SCRATCH_OPERATOR_GUIDE.md)

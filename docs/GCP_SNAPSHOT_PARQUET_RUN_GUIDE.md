# GCP Snapshot Parquet Run Guide

This is the operator path for rebuilding the final historical parquet datasets on a high-power GCP machine and saving them to shared Cloud Storage.

Use this when you want:

- canonical historical `MarketSnapshot` parquet
- derived `snapshots_ml_flat` parquet for ML research
- a resumable build path from raw BankNifty archive
- outputs stored in GCS so they are accessible from other VMs and future runs

This is a snapshot-only path.

For this runbook you do not need:

- runtime image build
- runtime config publish
- a running runtime VM

If the GCP project is brand new, you only need the minimal project and API preparation from [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md). Do not follow the runtime/image steps for this lane.

## 1. What This Produces

The snapshot pipeline writes two final datasets under `.data/ml_pipeline/parquet_data`:

- `snapshots/year=YYYY/data.parquet`
- `snapshots_ml_flat/year=YYYY/data.parquet`

The recommended GCS target is:

- raw archive prefix:
  - `gs://<snapshot-data-bucket>/banknifty_data`
- final parquet prefix:
  - `gs://<snapshot-data-bucket>/parquet_data`

## 2. Exact Bucket Values For This Project

For project `gen-lang-client-0909109011`, use these values in `ops/gcp/operator.env`:

```bash
SNAPSHOT_DATA_BUCKET_NAME="gen-lang-client-0909109011-option-trading-snapshots"
RAW_ARCHIVE_BUCKET_URL="gs://gen-lang-client-0909109011-option-trading-snapshots/banknifty_data"
SNAPSHOT_PARQUET_BUCKET_URL="gs://gen-lang-client-0909109011-option-trading-snapshots/parquet_data"
```

These are the values the rest of this runbook assumes.

## 3. Minimal Snapshot-Only Setup

For snapshot build only, do this minimal setup in Cloud Shell:

```bash
gcloud config set project gen-lang-client-0909109011
gcloud services enable \
  compute.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com
```

Create the shared snapshot bucket directly:

```bash
gcloud storage buckets create \
  "gs://gen-lang-client-0909109011-option-trading-snapshots" \
  --location="asia-south1" \
  --uniform-bucket-level-access
```

If the bucket already exists, that is fine. Continue.

You do not need to run `from_scratch_bootstrap.sh` for this snapshot-only lane.

## 4. Upload Raw Archive Once

From a machine that has the raw archive locally:

```bash
export RAW_ARCHIVE_BUCKET_URL="gs://gen-lang-client-0909109011-option-trading-snapshots/banknifty_data"
export RAW_DATA_ROOT="/path/to/banknifty_data"
./ops/gcp/publish_raw_market_data.sh
```

Expected raw root layout:

- `banknifty_fut`
- `banknifty_options`
- `banknifty_spot`
- `VIX`

## 5. Create The Temporary High-Power Snapshot VM

Do the full rebuild on a disposable Linux VM with enough CPU and disk.

Recommended starting point:

- `n2-highmem-32`
- at least `500 GB` balanced persistent disk

The default training template is still smaller. For a full multi-year historical rebuild, create a separate temporary VM for the snapshot build window, then delete it after upload.

Example create command:

```bash
gcloud compute instances create option-trading-snapshot-build-01 \
  --project "gen-lang-client-0909109011" \
  --zone "asia-south1-b" \
  --machine-type "n2-highmem-32" \
  --boot-disk-size "500GB" \
  --boot-disk-type "pd-balanced" \
  --image-family "ubuntu-2204-lts" \
  --image-project "ubuntu-os-cloud" \
  --scopes "https://www.googleapis.com/auth/cloud-platform"
```

If `asia-south1-b` does not have capacity, retry in another zone that has capacity and update the guide commands accordingly.

## 6. Prepare The Snapshot VM

SSH to the VM:

```bash
gcloud compute ssh option-trading-snapshot-build-01 --zone "asia-south1-b"
```

On that VM:

```bash
gcloud config set project gen-lang-client-0909109011
sudo apt-get update
sudo apt-get install -y git python3-venv
git clone https://github.com/amitsajwan/option_trading.git
cd ~/option_trading
git checkout chore/ml-pipeline-ubuntu-gcp-runbook
git pull --ff-only
```

Create `ops/gcp/operator.env` on the VM and make sure it contains at least these snapshot values:

```bash
PROJECT_ID="gen-lang-client-0909109011"
REGION="asia-south1"
ZONE="asia-south1-b"
REPO_CLONE_URL="https://github.com/amitsajwan/option_trading.git"
REPO_REF="chore/ml-pipeline-ubuntu-gcp-runbook"
SNAPSHOT_DATA_BUCKET_NAME="gen-lang-client-0909109011-option-trading-snapshots"
RAW_ARCHIVE_BUCKET_URL="gs://gen-lang-client-0909109011-option-trading-snapshots/banknifty_data"
SNAPSHOT_PARQUET_BUCKET_URL="gs://gen-lang-client-0909109011-option-trading-snapshots/parquet_data"
```

No runtime bucket, model bucket, runtime image tag, or runtime config values are required for this runbook.

## 7. Build And Publish Final Parquet

On the temporary high-power GCP VM:

```bash
cd ~/option_trading
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

## 8. Useful Variants

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

## 9. Published Layout In GCS

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

## 10. Resume Strategy

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

## 11. Verification Commands

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
gcloud storage ls "gs://gen-lang-client-0909109011-option-trading-snapshots/parquet_data/**"
```

## 12. Delete The Temporary Snapshot VM

After parquet and reports are safely in GCS:

```bash
gcloud compute instances delete option-trading-snapshot-build-01 --zone "asia-south1-b" --quiet
```

## 13. Related Files

- [ops/gcp/README.md](../ops/gcp/README.md)
- [snapshot_app/historical/README.md](../snapshot_app/historical/README.md)
- [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md)
- [FROM_SCRATCH_OPERATOR_GUIDE.md](FROM_SCRATCH_OPERATOR_GUIDE.md)

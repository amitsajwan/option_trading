# GCP Snapshot Parquet Run Guide

This is the operator path for rebuilding the final historical parquet datasets on a temporary GCP machine and saving them to shared Cloud Storage.

Use this when you want:

- canonical historical `MarketSnapshot` parquet
- derived `snapshots_ml_flat` parquet for ML research
- a resumable build path from raw BankNifty archive
- outputs stored in GCS so they are accessible from other VMs and future runs

This is a snapshot-only path.

For this runbook you do not need:

- Artifact Registry
- model bucket
- runtime-config bucket
- runtime VM
- training VM template
- runtime image build
- runtime config publish
- a running runtime VM

## Tooling Choice

For this runbook, use `gcloud` only.

Do not use Terraform for the snapshot-only lane unless you intentionally also want the full runtime and training base platform.

## Execution Summary

This guide uses:

1. `gcloud services enable`
2. `gcloud storage buckets create`
3. `gcloud compute instances create`
4. `gcloud compute ssh`
5. `gcloud storage rsync`
6. the repo's snapshot build scripts on the temporary VM

This guide does not use:

- `terraform init`
- `terraform apply`
- `from_scratch_bootstrap.sh`
- runtime image build
- runtime config publish

If the GCP project is brand new, you only need the minimal project and API preparation from [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md). Do not follow the runtime/image steps for this lane.

## 1. What This Produces

Resources created or used in this phase:

- one shared snapshot data bucket
- one temporary snapshot-build VM

Resources intentionally not created in this phase:

- runtime VM
- training VM template
- Artifact Registry
- model bucket
- runtime-config bucket

The snapshot pipeline writes these final datasets under `.data/ml_pipeline/parquet_data`:

- `snapshots/**/data.parquet`
- `snapshots_ml_flat/**/data.parquet`
- `stage1_entry_view/**/data.parquet`
- `stage2_direction_view/**/data.parquet`
- `stage3_recipe_view/**/data.parquet`

The current builder writes chunked parquet partitions under each `year=YYYY` directory so it can parallelize below the year level without write conflicts.

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
You also do not need Terraform for this lane.

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

Current local archive layout observed under `C:\code\banknifty_data`:

- futures, options, and spot year folders: `2020`, `2021`, `2022`, `2023`, `2024`
- VIX files:
  - `2020`
  - `2021-03-31` through `2021-12-31`
  - `2022`
  - `2023`
  - `2024-01-01` through `2024-12-01`

That means the practical full-archive rebuild for the current raw set is `2020` through `2024`, with the VIX coverage above.

## 5. Create The Temporary Snapshot VM

Do the full rebuild on a disposable Linux VM with enough CPU and disk.

Recommended starting point:

- `n2-highmem-8`
- `300 GB` balanced persistent disk

Why this is the current default:

- the current snapshot build parallelizes with chunked calendar slices plus warmup continuity
- the default builder settings use:
  - `SNAPSHOT_JOBS=8`
  - `SNAPSHOT_SLICE_MONTHS=6`
  - `SNAPSHOT_SLICE_WARMUP_DAYS=90`
- `n2-highmem-8` is a good current balance of CPU, RAM, and disk for a full clean rebuild

The default training template is still smaller. For a full multi-year historical rebuild, create a separate temporary VM for the snapshot build window, then delete it after upload.

Example create command:

```bash
gcloud compute instances create option-trading-snapshot-build-01 \
  --project "gen-lang-client-0909109011" \
  --zone "asia-south1-b" \
  --machine-type "n2-highmem-8" \
  --boot-disk-size "300GB" \
  --boot-disk-type "pd-balanced" \
  --image-family "ubuntu-2204-lts" \
  --image-project "ubuntu-os-cloud" \
  --scopes "https://www.googleapis.com/auth/cloud-platform"
```

If `asia-south1-b` does not have capacity, retry in another zone that has capacity and update the guide commands accordingly.
If you keep other `pd-balanced` disks in the same region, reduce the boot disk size further or delete unused VMs first to stay within `SSD_TOTAL_GB` quota.

Delete this VM after parquet upload. It is not part of the long-lived platform.

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

### Clean Rebuild Requirement

The fast parallel builder now writes chunked parquet under each year directory.

If this VM or parquet base already contains old legacy snapshot outputs like:

- `snapshots/year=YYYY/data.parquet`
- `snapshots_ml_flat/year=YYYY/data.parquet`

delete those snapshot output roots before starting the new full build:

```bash
cd ~/option_trading
rm -rf .data/ml_pipeline/parquet_data/snapshots
rm -rf .data/ml_pipeline/parquet_data/snapshots_ml_flat
rm -rf .data/ml_pipeline/parquet_data/stage1_entry_view
rm -rf .data/ml_pipeline/parquet_data/stage2_direction_view
rm -rf .data/ml_pipeline/parquet_data/stage3_recipe_view
```

Do not delete the normalized cache (`futures`, `options`, `spot`, `vix`) unless you intentionally want to rebuild Layer-1 too.

### Full Current Archive

To process the full raw archive currently present in GCS, do not set `YEAR`, `MIN_DAY`, or `MAX_DAY`.

This will process all available raw data under:

- `banknifty_fut/2020` through `banknifty_fut/2024`
- `banknifty_options/2020` through `banknifty_options/2024`
- `banknifty_spot/2020` through `banknifty_spot/2024`
- all available `VIX` files

Run:

```bash
cd ~/option_trading
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export VALIDATE_DAYS=5
unset YEAR
unset MIN_DAY
unset MAX_DAY
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

### Clean Full Rebuild On An Existing VM

If this VM already contains old snapshot outputs or a partial interrupted run, clean only the final snapshot output roots and restart from scratch:

```bash
cd ~/option_trading
rm -rf .data/ml_pipeline/parquet_data/snapshots
rm -rf .data/ml_pipeline/parquet_data/snapshots_ml_flat
rm -rf .data/ml_pipeline/parquet_data/stage1_entry_view
rm -rf .data/ml_pipeline/parquet_data/stage2_direction_view
rm -rf .data/ml_pipeline/parquet_data/stage3_recipe_view

export SYNC_RAW_ARCHIVE_FROM_GCS=0
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export VALIDATE_DAYS=5
unset YEAR
unset MIN_DAY
unset MAX_DAY
export NO_RESUME=1
nohup ./ops/gcp/run_snapshot_parquet_pipeline.sh > snapshot_full_run.log 2>&1 &
```

Monitor the run:

```bash
tail -f snapshot_full_run.log
```

### Full Current Archive By Explicit Year

If you prefer to drive the current archive explicitly by year, run these one at a time:

```bash
cd ~/option_trading
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=1
unset SNAPSHOT_SLICE_MONTHS
unset SNAPSHOT_SLICE_WARMUP_DAYS
for YEAR in 2020 2021 2022 2023 2024; do
  export YEAR
  ./ops/gcp/run_snapshot_parquet_pipeline.sh
done
unset YEAR
```

### General Build Command

On the temporary snapshot VM:

```bash
cd ~/option_trading
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export VALIDATE_DAYS=5
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

What this does:

1. syncs raw archive from `RAW_ARCHIVE_BUCKET_URL` to local disk
2. creates or reuses `.venv`
3. installs `snapshot_app` build dependencies
4. runs `snapshot_app.historical.snapshot_batch_runner`
5. partitions the archive into chunked calendar slices with warmup continuity
6. writes:
   - build manifest
   - validation report
   - latest window manifest
7. uploads final parquet and reports to `SNAPSHOT_PARQUET_BUCKET_URL`

## 8. Useful Variants

Build one year only:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export SNAPSHOT_JOBS=1
unset SNAPSHOT_SLICE_MONTHS
unset SNAPSHOT_SLICE_WARMUP_DAYS
export YEAR=2024
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Build a small date slice:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
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
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export YEAR=2024
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Also upload normalized parquet cache:

```bash
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export PUBLISH_NORMALIZED_CACHE=1
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

## 9. Published Layout In GCS

After a successful upload, the bucket prefix should contain:

- `parquet_data/snapshots/**/data.parquet`
- `parquet_data/snapshots_ml_flat/**/data.parquet`
- `parquet_data/stage1_entry_view/**/data.parquet`
- `parquet_data/stage2_direction_view/**/data.parquet`
- `parquet_data/stage3_recipe_view/**/data.parquet`
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

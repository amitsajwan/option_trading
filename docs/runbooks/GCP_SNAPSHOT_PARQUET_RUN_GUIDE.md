# Snapshot Creation Runbook

Use this runbook to build historical parquet on GCP and publish it for later training or replay use.

This workflow is self-contained. It includes the GCP setup it needs.

## What This Produces

- raw archive in GCS
- canonical historical `snapshots`
- `market_base`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`

## Step 1: Prepare GCP For Snapshot Build

From Cloud Shell or another machine with `gcloud`:

```bash
gcloud config set project "${PROJECT_ID}"
gcloud services enable \
  compute.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com
```

Verify:

- `gcloud config get-value project` returns the expected project
- the `gcloud services enable` command exits without error

## Step 2: Create Or Confirm The Snapshot Bucket

Choose a bucket for raw archive and final parquet.

Example:

```bash
gcloud storage buckets create \
  "gs://${SNAPSHOT_DATA_BUCKET_NAME}" \
  --location="${REGION}" \
  --uniform-bucket-level-access
```

If the bucket already exists, continue.

Verify:

```bash
gcloud storage ls "gs://${SNAPSHOT_DATA_BUCKET_NAME}"
```

Look for:

- the bucket is listed

## Step 3: Upload Raw Archive If Needed

From a machine that already has the raw BankNifty archive locally:

```bash
export RAW_ARCHIVE_BUCKET_URL="gs://${SNAPSHOT_DATA_BUCKET_NAME}/banknifty_data"
export RAW_DATA_ROOT="/path/to/banknifty_data"
./ops/gcp/publish_raw_market_data.sh
```

Verify:

```bash
gcloud storage ls "${RAW_ARCHIVE_BUCKET_URL}"
```

Look for:

- `banknifty_fut`
- `banknifty_options`
- `banknifty_spot`
- `VIX`

## Step 4: Create The Temporary Snapshot VM

Create a disposable high-power VM for the build.

Example:

```bash
gcloud compute instances create option-trading-snapshot-build-01 \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --machine-type "n2-highmem-8" \
  --boot-disk-size "300GB" \
  --boot-disk-type "pd-balanced" \
  --image-family "ubuntu-2204-lts" \
  --image-project "ubuntu-os-cloud" \
  --scopes "https://www.googleapis.com/auth/cloud-platform"
```

Verify:

```bash
gcloud compute instances describe option-trading-snapshot-build-01 \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- `RUNNING`

## Step 5: Prepare The VM

SSH to the VM and prepare the repo:

```bash
gcloud compute ssh option-trading-snapshot-build-01 --zone "${ZONE}"
```

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv
git clone <repo-clone-url>
cd option_trading
git checkout <repo-ref>
git pull --ff-only
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

Set at least these values in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `RAW_ARCHIVE_BUCKET_URL`
- `SNAPSHOT_PARQUET_BUCKET_URL`
- `SNAPSHOT_DATA_BUCKET_NAME`

Verify:

```bash
grep -E "RAW_ARCHIVE_BUCKET_URL|SNAPSHOT_PARQUET_BUCKET_URL|SNAPSHOT_DATA_BUCKET_NAME" ops/gcp/operator.env
```

Look for:

- all three variables are present and non-empty

## Step 6: Check Source Coverage Before Backfill

Snapshots only build for days that have normalized futures input and option-chain input.
If futures or spot extend farther than options, the snapshot builder will stop at the option coverage boundary.

On the snapshot VM:

```bash
find .data/ml_pipeline/parquet_data/options -maxdepth 2 -type d | sort
find .data/ml_pipeline/parquet_data/futures -maxdepth 2 -type d | sort
find .data/ml_pipeline/parquet_data/spot -maxdepth 2 -type d | sort
```

Look for:

- the target backfill years exist under `options`
- the target backfill years exist under `futures`
- the target backfill years exist under `spot`

If the target years are missing under `options`, upload the missing raw archive first and rerun normalization before starting snapshot backfill.

For a precise gap audit, run:

```bash
cd ~/option_trading
source .venv/bin/activate

python - <<'PY'
from snapshot_app.historical.parquet_store import ParquetStore

s = ParquetStore(".data/ml_pipeline/parquet_data", snapshots_dataset="snapshots")

futures_days = set(s.available_days())
option_days = set(s.all_days_with_options())
built_days = set(s.available_snapshot_days())

buildable_missing = sorted((futures_days & option_days) - built_days)
source_missing = sorted(futures_days - option_days)

print("futures_days:", len(futures_days), min(futures_days) if futures_days else None, max(futures_days) if futures_days else None)
print("option_days:", len(option_days), min(option_days) if option_days else None, max(option_days) if option_days else None)
print("built_days:", len(built_days), min(built_days) if built_days else None, max(built_days) if built_days else None)

print("\nbuildable_missing_count:", len(buildable_missing))
print("buildable_missing_first_20:", buildable_missing[:20])
print("buildable_missing_last_20:", buildable_missing[-20:])

print("\nsource_missing_count:", len(source_missing))
print("source_missing_first_20:", source_missing[:20])
print("source_missing_last_20:", source_missing[-20:])
PY
```

Interpretation:

- `buildable_missing_count > 0`: inputs exist and only the snapshot build is missing
- `source_missing_count > 0`: options coverage is missing, so rerunning the snapshot builder alone will not recover those days

## Step 7: Run The Snapshot Pipeline

On the snapshot VM:

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

Verify:

- command exits successfully
- final output includes `Snapshot parquet pipeline complete`
- final output prints:
  - `build run id`
  - `parquet base`
  - `report root`

Also verify local outputs:

```bash
find .data/ml_pipeline/parquet_data -maxdepth 2 -type d | sort
```

Look for:

- `snapshots`
- `market_base`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`

## Optional: One-Shot Autonomous Backfill And Publish

Use this instead of the manual backfill and publish steps below when you want the snapshot VM to handle sync, normalization, coverage audit, backfill, validation, and publish in one command.

This helper is designed for release-manager operation with no manual intervention after launch.
It will:

- sync the raw archive from GCS
- normalize raw data into local parquet
- audit source coverage vs built coverage
- stop early if raw options coverage is still missing
- backfill any newly buildable dates
- generate manifest and validation artifacts
- publish the finished parquet outputs
- optionally clean stale published dataset prefixes first

On the snapshot VM:

```bash
cd ~/option_trading
chmod +x ./ops/gcp/backfill_snapshot_parquet_and_publish.sh

set -a
source ops/gcp/operator.env
set +a

export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export VALIDATE_DAYS=5
./ops/gcp/backfill_snapshot_parquet_and_publish.sh
```

Optional targeting:

```bash
export YEAR=2022
./ops/gcp/backfill_snapshot_parquet_and_publish.sh
```

```bash
export MIN_DAY=2021-06-01
export MAX_DAY=2021-09-30
./ops/gcp/backfill_snapshot_parquet_and_publish.sh
```

Optional clean republish when chunk layout changed:

```bash
export CLEAN_PUBLISH_PREFIXES=1
./ops/gcp/backfill_snapshot_parquet_and_publish.sh
```

Verify:

- command exits successfully
- final output includes `Backfill and publish complete.`
- final output prints:
  - `run id`
  - `report root`
  - `parquet base`
  - `publish root`

## Step 8: Backfill Missing Years Or Date Gaps

Use resumable targeted runs instead of rebuilding everything.
Leave resume enabled by default so already-built days are skipped.

Plan a large missing range first:

```bash
python -m snapshot_app.historical.snapshot_batch_runner \
  --base .data/ml_pipeline/parquet_data \
  --build-stage all \
  --min-day 2022-01-01 \
  --max-day 2024-12-31 \
  --validate-ml-flat-contract \
  --validate-days 5 \
  --plan-year-runs
```

Backfill one whole calendar year:

```bash
cd ~/option_trading
unset MIN_DAY MAX_DAY
export YEAR=2022
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export VALIDATE_DAYS=5
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Backfill one specific missing date gap:

```bash
cd ~/option_trading
unset YEAR
export MIN_DAY=2021-06-01
export MAX_DAY=2021-09-30
export SYNC_RAW_ARCHIVE_FROM_GCS=1
export NORMALIZE_JOBS=8
export SNAPSHOT_JOBS=8
export SNAPSHOT_SLICE_MONTHS=6
export SNAPSHOT_SLICE_WARMUP_DAYS=90
export VALIDATE_DAYS=5
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Notes:

- use year-scoped runs when an entire year is missing
- use `MIN_DAY` and `MAX_DAY` when only one gap is missing
- do not set `NO_RESUME=1` for normal backfills
- if the build prints `days_available: 0` or still does not include the missing range, the source archive for that range is still missing or not normalized under `options`

## Step 9: Publish Existing Local Parquet Without Rebuild

If the local parquet tree is already complete and you only need manifest generation, validation, and publish, use the direct runner plus the publish script.
This is also the safe path when the wrapper run would return `already_complete`.

```bash
cd ~/option_trading
source .venv/bin/activate
set -a
source ops/gcp/operator.env
set +a

export REPO_ROOT=~/option_trading
export PARQUET_BASE="${REPO_ROOT}/.data/ml_pipeline/parquet_data"
export BUILD_RUN_ID="snapshot_publish_$(date -u +%Y%m%dT%H%M%SZ)"
export REPORT_ROOT="${REPO_ROOT}/.run/snapshot_parquet/${BUILD_RUN_ID}"

python -m snapshot_app.historical.snapshot_batch_runner \
  --base "${PARQUET_BASE}" \
  --build-stage all \
  --build-source historical \
  --validate-ml-flat-contract \
  --validate-days 5 \
  --manifest-out "${REPORT_ROOT}/build_manifest.json" \
  --validation-report-out "${REPORT_ROOT}/validation_report.json" \
  --window-manifest-out "${REPORT_ROOT}/window_manifest_latest.json"

./ops/gcp/publish_snapshot_parquet.sh
```

Important:

- `publish_snapshot_parquet.sh` expects `SNAPSHOT_PARQUET_BUCKET_URL` to be exported in the current shell
- `source ops/gcp/operator.env` alone is not enough if the shell variables are not exported to child processes; use `set -a` before sourcing

## Step 10: Clean Published Dataset Prefixes Before Republish When Chunk Layout Changed

The publish script uses `gcloud storage rsync` and does not delete stale remote files.
If the bucket already contains older chunk layouts, republishing can leave overlapping `chunk=*` parquet files in place.

Clean the published dataset prefixes first when either of these is true:

- this is the first publish after moving from legacy yearly files to chunked files
- you are rebuilding an already-published year with different chunk boundaries

```bash
cd ~/option_trading
source .venv/bin/activate
set -a
source ops/gcp/operator.env
set +a

for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
  gcloud storage rm --recursive "${SNAPSHOT_PARQUET_BUCKET_URL}/${ds}"
done

./ops/gcp/publish_snapshot_parquet.sh
```

## Step 11: Verify Published GCS Outputs

Verify:

```bash
gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL}/**"
```

Look for:

- `snapshots`
- `market_base`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`
- `reports/build_manifest.json`
- `reports/validation_report.json`
- `reports/window_manifest_latest.json`

Also verify that each published dataset only contains the expected chunk files and no overlapping old chunk layouts:

```bash
for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
  echo "== ${ds}"
  gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL}/${ds}/**" | grep 'data.parquet$' | sort
done
```

Look for:

- one `data.parquet` per expected published chunk
- no duplicate monthly and six-month chunk files for the same date range

If you see both monthly chunks like `chunk=202110_202110_m1` and six-month chunks like `chunk=202110_202112_m6` under the same dataset/year, clean the published dataset prefixes and republish before using the bucket for training.

## Step 12: Delete Temporary Snapshot Infra

Delete the temporary snapshot VM after publish is complete:

```bash
gcloud compute instances delete option-trading-snapshot-build-01 --zone "${ZONE}" --quiet
```

Verify:

```bash
gcloud compute instances describe option-trading-snapshot-build-01 \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- instance not found

Optional cleanup:

- if the snapshot bucket was created only for a one-off run and is no longer needed, delete it separately
- do not delete it if training or replay still needs the parquet outputs

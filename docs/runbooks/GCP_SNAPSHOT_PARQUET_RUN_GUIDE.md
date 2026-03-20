# Snapshot Creation Runbook

Use this runbook to build historical parquet on GCP and publish it for later training or replay use.

This runbook is centered on one supported operator command:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

The script can take a local raw `banknifty_data` archive all the way to final GCS publish in one run.

## What This Produces

- raw archive in GCS
- canonical historical `snapshots`
- `market_base`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`
- `reports/build_manifest.json`
- `reports/validation_report.json`
- `reports/window_manifest_latest.json`
- `reports/coverage_audit.json`

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

## Step 3: Create The Snapshot Build VM

Create a disposable high-power VM.
Use a machine with at least `16` vCPU so the pipeline can use up to `16` worker processes by default.

Example:

```bash
gcloud compute instances create option-trading-snapshot-build-01 \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --machine-type "n2-highmem-16" \
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

## Step 4: Prepare The VM

SSH to the VM:

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

Optional but recommended when the raw archive already exists on the VM:

- `LOCAL_RAW_ARCHIVE_ROOT`

The expected raw archive layout is:

- `banknifty_fut`
- `banknifty_options`
- `banknifty_spot`
- `VIX` or `vix`

Verify:

```bash
grep -E "RAW_ARCHIVE_BUCKET_URL|SNAPSHOT_PARQUET_BUCKET_URL|SNAPSHOT_DATA_BUCKET_NAME|LOCAL_RAW_ARCHIVE_ROOT" ops/gcp/operator.env
```

Look for:

- bucket URLs are present and non-empty
- `LOCAL_RAW_ARCHIVE_ROOT` is set only if the raw archive is already on this VM

## Step 5: Run The Single Snapshot Build And Publish Script

On the snapshot VM:

```bash
cd ~/option_trading
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

What the script does:

1. uploads `LOCAL_RAW_ARCHIVE_ROOT` to `RAW_ARCHIVE_BUCKET_URL` when `LOCAL_RAW_ARCHIVE_ROOT` is set
2. syncs `RAW_ARCHIVE_BUCKET_URL` into the VM cache under `.cache/banknifty_data`
3. creates or reuses `.venv`
4. installs `snapshot_app` requirements if needed
5. normalizes raw futures, options, spot, and VIX into `.data/ml_pipeline/parquet_data`
6. audits source coverage vs built coverage and writes `coverage_audit.json`
7. fails closed if source options coverage is missing
8. builds only pending snapshot and derived days with resume enabled by default
9. regenerates manifest and validation reports even when the local parquet tree is already complete
10. cleans remote GCS prefixes by default
11. publishes datasets and reports to `SNAPSHOT_PARQUET_BUCKET_URL`
12. verifies the published GCS layout

Notes:

- worker defaults auto-detect CPU and cap at `16`
- rerunning the same command resumes from the existing local parquet state
- the script publishes only after the local build and validation state is publishable, unless `ALLOW_PARTIAL_PUBLISH=1` is set explicitly

Verify:

- command exits successfully
- final output includes `Snapshot parquet pipeline complete.`
- final output prints:
  - `build run id`
  - `parquet base`
  - `report root`
  - `raw source gcs`
  - `raw cache`
  - `normalize jobs`
  - `snapshot jobs`
  - `publish root`

## Step 6: Verify Published GCS Outputs

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
- `reports/coverage_audit.json`

Also verify that each published dataset only contains the expected chunk files:

```bash
for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
  echo "== ${ds}"
  gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL}/${ds}/**" | grep 'data.parquet$' | sort
done
```

Look for:

- one `data.parquet` per expected published chunk
- no duplicate old and new chunk layouts for the same date range

## Troubleshooting Appendix

Use these only when the one-shot script failed and you need a more direct diagnostic or recovery path.

### Check Coverage Directly

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
print("buildable_missing_count:", len(buildable_missing))
print("source_missing_count:", len(source_missing))
PY
```

Interpretation:

- `buildable_missing_count > 0`: inputs exist and rerunning the main script should backfill those days
- `source_missing_count > 0`: raw or normalized options coverage is missing and must be fixed before publish

### Rerun One Targeted Year Or Date Window

```bash
cd ~/option_trading
export YEAR=2022
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

```bash
cd ~/option_trading
export MIN_DAY=2021-06-01
export MAX_DAY=2021-09-30
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Notes:

- leave resume enabled by default
- do not set `NO_RESUME=1` for normal recovery

### Publish Existing Local Parquet Without Rebuild

If the local parquet tree is complete and you only need fresh reports plus a republish:

```bash
cd ~/option_trading
source .venv/bin/activate
set -a
source ops/gcp/operator.env
set +a

export REPO_ROOT="${REPO_ROOT:-$HOME/option_trading}"
export PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
export REPORT_ROOT="${REPO_ROOT}/.run/snapshot_parquet/snapshot_publish_$(date -u +%Y%m%dT%H%M%SZ)"

python -m snapshot_app.historical.snapshot_batch_runner \
  --base "${PARQUET_BASE}" \
  --build-stage all \
  --build-source historical \
  --validate-ml-flat-contract \
  --validate-days 5 \
  --manifest-out "${REPORT_ROOT}/build_manifest.json" \
  --validation-report-out "${REPORT_ROOT}/validation_report.json" \
  --window-manifest-out "${REPORT_ROOT}/window_manifest_latest.json"

for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view reports; do
  gcloud storage rm --recursive "${SNAPSHOT_PARQUET_BUCKET_URL}/${ds}" || true
done

./ops/gcp/publish_snapshot_parquet.sh
```

### Seed GCS Raw Archive Outside The Main Script

If the raw archive lives on another machine and you want to pre-stage it in GCS:

```bash
export RAW_ARCHIVE_BUCKET_URL="gs://${SNAPSHOT_DATA_BUCKET_NAME}/banknifty_data"
export RAW_DATA_ROOT="/path/to/banknifty_data"
./ops/gcp/publish_raw_market_data.sh
```

## Step 7: Delete Temporary Snapshot Infra

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

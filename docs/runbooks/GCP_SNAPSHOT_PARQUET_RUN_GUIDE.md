# Snapshot Creation Runbook

Use this runbook to build historical parquet on GCP and publish it for later training or replay use.

This runbook is centered on one supported operator command:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

The script can take a local raw `banknifty_data` archive all the way to final GCS publish in one run.

Host rule:

- use Linux only for `run_snapshot_parquet_pipeline.sh`
- supported hosts are Ubuntu, Cloud Shell, and WSL
- do not run the full parquet wrapper from Windows Git Bash; use Windows only to seed the raw archive into GCS
- Cloud Shell is suitable for orchestration, but not for the full parquet build when local disk is small
- prefer a dedicated Linux VM with `150GB+` free disk, ideally a `300GB` boot disk for full rebuilds

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

From Cloud Shell, Ubuntu, or WSL:

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

If your raw archive currently lives on a Windows machine, seed it into GCS from Windows first:

```bash
RAW_ARCHIVE_BUCKET_URL="gs://${SNAPSHOT_DATA_BUCKET_NAME}/banknifty_data" ./ops/gcp/publish_raw_market_data.sh /path/to/banknifty_data
```

Then switch to the Linux snapshot-build host for the remaining steps.

Verify:

```bash
gcloud storage ls "gs://${SNAPSHOT_DATA_BUCKET_NAME}"
```

Look for:

- the bucket is listed

## Step 3: Create The Snapshot Build VM

Create a disposable high-power VM.
Use a machine with enough local disk for the raw cache plus parquet outputs. `8` to `16` vCPU with `16GB+` RAM is a practical range, but disk is the first constraint here.

Minimum recommendation:

- `150GB+` free disk before the run starts
- `300GB` boot disk for a full rebuild

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
- enough free disk for the build

Also verify free disk immediately after SSH:

```bash
df -h /
```

If free disk is under `150G`, stop and resize or switch hosts before running the wrapper.

## Step 4: Prepare The VM

SSH to the VM:

```bash
gcloud compute ssh option-trading-snapshot-build-01 --zone "${ZONE}"
```

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv tmux
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

If the raw archive was already seeded into GCS from another machine, leave `LOCAL_RAW_ARCHIVE_ROOT` empty on the snapshot-build VM. The wrapper will use `RAW_ARCHIVE_BUCKET_URL` as the canonical source and sync it into the local cache.

Required for the one-command snapshot flow:

- `RAW_ARCHIVE_BUCKET_URL`
- `SNAPSHOT_PARQUET_BUCKET_URL`
- `REPO_ROOT` should point at the checkout on this VM when it is not `/opt/option_trading`

Recommended worker defaults for recovery:

- `NORMALIZE_JOBS=1` for a clean retry after a partial normalize error
- `SNAPSHOT_JOBS=2` for the first replay after normalization succeeds
- `NO_RESUME=1` only when you have deleted the local parquet tree and need a clean rebuild

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

For long-running snapshot builds, always use `tmux`.
If the SSH session drops while the script is running in a plain foreground shell, the build usually stops with the shell.

Basic `tmux` commands:

```bash
tmux new -s snapshot
tmux attach -t snapshot
tmux ls
```

Detach from `tmux` without stopping the build:

- press `Ctrl+b`
- then press `d`

## Step 5: Run The Single Snapshot Build And Publish Script

On the snapshot VM:

```bash
cd ~/option_trading
tmux new -s snapshot
```

Inside the `tmux` session:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
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

- worker defaults are now host-aware and scale up on dedicated Linux build VMs
- you can still override them explicitly, for example `NORMALIZE_JOBS=8 SNAPSHOT_JOBS=4`
- rerunning the same command resumes from the existing local parquet state
- the script publishes only after the local build and validation state is publishable, unless `ALLOW_PARTIAL_PUBLISH=1` is set explicitly
- the wrapper fails before publish if normalization returns `partial_error`
- the wrapper fails before publish if `stage2_direction_view` is missing any columns from `STAGE2_REQUIRED_COLUMNS`
- follow live progress from another SSH session with `tail -f ~/option_trading/snapshot-run.log`
- if you disconnect, reconnect and run `tmux attach -t snapshot`

If normalization reports `partial_error` or leaves a corrupt parquet partition behind:

1. stop the wrapper
2. delete the local parquet cache under `"$REPO_ROOT/.data/ml_pipeline/parquet_data"`
3. rerun with `NO_RESUME=1 NORMALIZE_JOBS=1 SNAPSHOT_JOBS=2`
4. verify the local tree again before publishing anything

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

Before training, confirm the rebuilt Stage 2 view exposes the new direction features:

```bash
python - <<'PY'
import os
from pathlib import Path
import pandas as pd

root = Path(os.environ["REPO_ROOT"]) / ".data/ml_pipeline/parquet_data/stage2_direction_view"
sample = next(root.rglob("*.parquet"))
required = [
    "pcr_change_5m",
    "pcr_change_15m",
    "atm_oi_ratio",
    "near_atm_oi_ratio",
    "atm_ce_oi",
    "atm_pe_oi",
]
df = pd.read_parquet(sample, columns=required)
print(sample)
print(df.notna().mean().to_string())
PY
```

If any required column is missing, rerun the snapshot wrapper instead of starting training.

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

### Recover After SSH Disconnect

Check whether the wrapper or publish step is still running:

```bash
cd ~/option_trading
pgrep -af "run_snapshot_parquet_pipeline.sh|snapshot_batch_runner|publish_snapshot_parquet.sh"
```

Inspect the latest run directory:

```bash
cd ~/option_trading
LATEST="$(ls -1dt .run/snapshot_parquet/* 2>/dev/null | head -n 1)"
echo "${LATEST}"
ls -lah "${LATEST}"
```

Interpretation:

- active process found: the build is still running; reattach with `tmux attach -t snapshot` or watch `tail -f ~/option_trading/snapshot-run.log`
- no active process and only `coverage_audit.json` exists: the run stopped before build completion; rerun the main script
- `build_manifest.json` exists locally but GCS is still empty or partial: the run reached local report generation but did not finish publish; rerun the main script

The main script is resumable by default, so the standard recovery path is:

```bash
cd ~/option_trading
tmux attach -t snapshot
```

If no `snapshot` session exists yet:

```bash
cd ~/option_trading
tmux new -s snapshot
```

Inside the `tmux` session, rerun:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
```

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

This is a manual recovery path, not the normal operator path.
It is not equivalent to `run_snapshot_parquet_pipeline.sh`.

Important differences from the supported wrapper:

- it does not run the wrapper's source-coverage audit gate before publish
- it does not verify that the requested build window is fully closed before publish
- it can republish incomplete local parquet if you skip the checks below

Use it only when you have already verified that the local parquet tree is complete and consistent for the intended publish window.

Minimum checks before manual republish:

```bash
cd ~/option_trading
LATEST_REPORT="$(ls -1dt .run/snapshot_parquet/* 2>/dev/null | head -n 1)"
echo "${LATEST_REPORT}"
test -f "${LATEST_REPORT}/coverage_audit.json" && cat "${LATEST_REPORT}/coverage_audit.json"
test -f "${LATEST_REPORT}/build_manifest.json" && cat "${LATEST_REPORT}/build_manifest.json"
```

Look for:

- `source_missing_count` is `0`
- `buildable_missing_count` is `0`
- `build_manifest.json` reports a publishable local build state

Only then use the manual republish sequence below:

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

If any of the checks above fail, stop and rerun the supported wrapper instead:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
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

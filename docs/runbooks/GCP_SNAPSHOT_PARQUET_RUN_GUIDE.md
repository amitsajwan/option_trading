# Snapshot/Parquet Build Runbook

Use this runbook when you need to build or rebuild historical parquet on GCP and publish it for:

- staged ML training
- historical replay
- downstream snapshot-derived research datasets

This runbook is intentionally separate from the runtime lifecycle menu. The supported operator entrypoint for snapshot/parquet build is:

```bash
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Do not use `runtime_lifecycle_interactive.sh` as the main entrypoint for parquet creation. That menu covers `Infra`, `Live`, and `Historical replay`, but the parquet artifact build itself is a separate wrapper with its own host and disk requirements.

## What The Wrapper Produces

The wrapper builds and publishes:

- canonical `snapshots`
- `market_base`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`
- `reports/build_manifest.json`
- `reports/validation_report.json`
- `reports/window_manifest_latest.json`
- `reports/coverage_audit.json`

It also fails closed before publish when:

- raw/options source coverage is incomplete for the requested window
- the local build does not close the requested window
- the local build manifest is not publishable
- `stage2_direction_view` is missing any column from `STAGE2_REQUIRED_COLUMNS`

## Host Constraints

Use these rules as hard constraints:

- run `run_snapshot_parquet_pipeline.sh` from Linux only
- supported hosts are Ubuntu, GCP VM Linux shells, Cloud Shell, and WSL
- do not run the full wrapper from Windows Git Bash
- use Windows only for raw archive seeding via `publish_raw_market_data.sh`
- do not use Cloud Shell as the full build host when disk is limited
- prefer a dedicated snapshot-build VM instead of the runtime VM

Practical build-host minimums:

- `150G+` free disk before the run starts
- `300G` boot disk for a fresh full rebuild
- `8-16` vCPU and `16G+` RAM is a practical range

Cloud Shell is fine for:

- bucket setup
- `gcloud` orchestration
- raw archive upload to GCS

Cloud Shell is not the default host for:

- full raw-to-parquet rebuild
- long-running `tmux`-based publish runs

## Fresh Rebuild Flow

Use this as the current recommended path for a new environment or a clean historical rebuild.

### Step 1: Prepare GCP And Buckets

From Cloud Shell, Ubuntu, or WSL:

```bash
gcloud config set project "${PROJECT_ID}"
gcloud services enable \
  compute.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com
```

Create or confirm the snapshot bucket:

```bash
gcloud storage buckets create \
  "gs://${SNAPSHOT_DATA_BUCKET_NAME}" \
  --location="${REGION}" \
  --uniform-bucket-level-access
```

If the raw archive currently lives on another machine, pre-stage it into GCS first:

```bash
RAW_ARCHIVE_BUCKET_URL="gs://${SNAPSHOT_DATA_BUCKET_NAME}/banknifty_data" \
bash ./ops/gcp/publish_raw_market_data.sh /path/to/banknifty_data
```

Then move to the dedicated Linux snapshot-build host for the actual parquet build.

### Step 2: Create Or Choose The Snapshot-Build Host

Recommended disposable VM shape:

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

Verify after SSH:

```bash
df -h /
```

Stop and resize or switch hosts if free disk is under `150G`.

### Step 3: Prepare The Repo Checkout And `operator.env`

On the snapshot-build host:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv tmux
git clone <repo-clone-url>
cd option_trading
git checkout <repo-ref>
git pull --ff-only
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

For the snapshot wrapper, make these values explicit in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `RAW_ARCHIVE_BUCKET_URL`
- `SNAPSHOT_PARQUET_BUCKET_URL`

Recommended values:

- `LOCAL_RAW_ARCHIVE_ROOT` if the raw archive already exists on this VM
- `NORMALIZE_JOBS`, `SNAPSHOT_JOBS`, `SNAPSHOT_SLICE_MONTHS`, `SNAPSHOT_SLICE_WARMUP_DAYS` only after a clean first run
- `STAGE2_REQUIRED_COLUMNS` when the training contract changes

If the raw archive was already uploaded to GCS from another machine:

- leave `LOCAL_RAW_ARCHIVE_ROOT` empty
- let the wrapper sync from `RAW_ARCHIVE_BUCKET_URL` into the local cache

If this is a fresh rebuild from raw files already on the VM:

- set `LOCAL_RAW_ARCHIVE_ROOT`
- the wrapper will upload that tree to `RAW_ARCHIVE_BUCKET_URL` first
- then it will sync back from GCS so the build always runs against the canonical bucket source

### Step 4: Start The Wrapper In `tmux`

On the snapshot-build host:

```bash
cd ~/option_trading
tmux new -s snapshot
```

Inside the `tmux` session:

```bash
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
```

Detach without stopping the build:

- `Ctrl+b`
- `d`

Reattach later:

```bash
tmux attach -t snapshot
```

### What The Wrapper Does

In order, the wrapper:

1. uploads `LOCAL_RAW_ARCHIVE_ROOT` into `RAW_ARCHIVE_BUCKET_URL` when `LOCAL_RAW_ARCHIVE_ROOT` is set
2. syncs `RAW_ARCHIVE_BUCKET_URL` into the local raw cache under `.cache/banknifty_data`
3. creates or reuses `.venv`
4. installs `snapshot_app` requirements only when they changed
5. normalizes raw futures, options, spot, and VIX into `.data/ml_pipeline/parquet_data`
6. writes `coverage_audit.json` before build
7. refuses to continue if options/source coverage is incomplete
8. builds only pending days by default with resume enabled
9. writes `build_manifest.json`, `validation_report.json`, and `window_manifest_latest.json`
10. re-audits coverage after build
11. refuses to publish if the requested window is still not closed
12. verifies the Stage 2 schema locally
13. cleans the remote published parquet prefixes by default
14. publishes datasets and reports to `SNAPSHOT_PARQUET_BUCKET_URL`
15. verifies the published layout in GCS

The wrapper writes run artifacts under:

```bash
.run/snapshot_parquet/${BUILD_RUN_ID}/
```

### Step 5: Verify Local And Published Outputs

The run is healthy when the final output includes:

- `Snapshot parquet pipeline complete.`
- `build run id`
- `parquet base`
- `report root`
- `raw source gcs`
- `raw cache`
- `normalize jobs`
- `snapshot jobs`
- `publish root`

Verify the published GCS layout:

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

Also verify only the expected chunk files exist:

```bash
for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
  echo "== ${ds}"
  gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL}/${ds}/**" | grep 'data.parquet$' | sort
done
```

Before training, confirm the rebuilt Stage 2 view exposes the expected direction features:

```bash
python - <<'PY'
import os
from pathlib import Path
import pandas as pd

root = Path(os.environ.get("REPO_ROOT", ".")) / ".data/ml_pipeline/parquet_data/stage2_direction_view"
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

If any required Stage 2 column is missing, rerun the snapshot wrapper instead of starting training.

### Step 6: Hand Off To The Next Lane

After parquet is published:

- use [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md) for smoke or production training
- use [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) for historical replay against the published parquet

Do not upload historical parquet into the runtime-config bucket. Parquet stays under `SNAPSHOT_PARQUET_BUCKET_URL`.

### Step 7: Delete The Temporary Build Host

After publish succeeds:

```bash
gcloud compute instances delete option-trading-snapshot-build-01 --zone "${ZONE}" --quiet
```

## Worker Tuning

Start with the wrapper defaults on the first clean run. They are host-aware:

- `NORMALIZE_JOBS`
  - `1` when CPU count is `<= 4`
  - otherwise `min(cpu - 2, 12)`
- `SNAPSHOT_JOBS`
  - `2` when CPU count is `<= 4`
  - otherwise `min(max(cpu - 2, 2), 6)`
- `SNAPSHOT_SLICE_MONTHS=6`
- `SNAPSHOT_SLICE_WARMUP_DAYS=90`

Recommended tuning rules:

- first clean run: do not override anything
- if the VM is stable and has spare CPU, raise `NORMALIZE_JOBS` first
- only raise `SNAPSHOT_JOBS` after a clean baseline run, because slice parallelism is the heavier correctness-sensitive part
- keep `SNAPSHOT_SLICE_MONTHS=6` and `SNAPSHOT_SLICE_WARMUP_DAYS=90` unless you have a measured reason to change them
- recovery run after suspected bad local cache: `NO_RESUME=1 NORMALIZE_JOBS=1 SNAPSHOT_JOBS=2`

Typical explicit override on a larger host:

```bash
NORMALIZE_JOBS=8 SNAPSHOT_JOBS=4 bash ./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
```

Targeted rebuild examples:

```bash
YEAR=2022 bash ./ops/gcp/run_snapshot_parquet_pipeline.sh
```

```bash
MIN_DAY=2021-06-01 MAX_DAY=2021-09-30 bash ./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Use `YEAR` or `MIN_DAY`/`MAX_DAY` only when you intentionally want a narrow rebuild window.

## Restart And Recovery Guidance

### Normal Restart

This is the standard path after SSH disconnect or host session loss:

```bash
cd ~/option_trading
tmux attach -t snapshot
```

If no `snapshot` session exists:

```bash
cd ~/option_trading
tmux new -s snapshot
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
```

The wrapper is resumable by default. For ordinary restarts:

- do not set `NO_RESUME=1`
- rerun the same command
- let the wrapper detect already-built days and finish the window

### Check Whether The Run Is Still Active

```bash
cd ~/option_trading
pgrep -af "run_snapshot_parquet_pipeline.sh|snapshot_batch_runner|publish_snapshot_parquet.sh"
```

Inspect the latest local run directory:

```bash
cd ~/option_trading
LATEST="$(ls -1dt .run/snapshot_parquet/* 2>/dev/null | head -n 1)"
echo "${LATEST}"
ls -lah "${LATEST}"
```

Interpretation:

- active process found: the run is still active; reattach to `tmux` or watch the log
- only `coverage_audit.json` exists: the run stopped before build completion; rerun the wrapper
- `build_manifest.json` exists locally but GCS publish is missing or partial: rerun the same wrapper command and let it resume

Follow live progress from another SSH session:

```bash
tail -f ~/option_trading/snapshot-run.log
```

### When To Use `NO_RESUME=1`

Use `NO_RESUME=1` only when you intentionally want a clean rebuild of the selected local window, for example:

- corrupt local parquet after a failed normalization/build attempt
- legacy yearly layout mixed with current chunked layout
- explicit operator decision to discard the local partially built tree

If normalization reports `partial_error` or you suspect a bad local cache:

1. stop the wrapper
2. delete the affected local parquet outputs
3. rerun with `NO_RESUME=1 NORMALIZE_JOBS=1 SNAPSHOT_JOBS=2`

Example clean local output reset:

```bash
rm -rf .data/ml_pipeline/parquet_data/snapshots \
       .data/ml_pipeline/parquet_data/market_base \
       .data/ml_pipeline/parquet_data/snapshots_ml_flat \
       .data/ml_pipeline/parquet_data/stage1_entry_view \
       .data/ml_pipeline/parquet_data/stage2_direction_view \
       .data/ml_pipeline/parquet_data/stage3_recipe_view
```

Then rerun:

```bash
NO_RESUME=1 NORMALIZE_JOBS=1 SNAPSHOT_JOBS=2 \
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
```

## Manual Recovery Paths

Use these only when the supported wrapper path has already produced a complete local tree and you intentionally need a lower-level action.

### Publish Existing Local Parquet Without Rebuild

This is not equivalent to the wrapper. It skips the wrapper's source-coverage and requested-window closure gates.

Minimum checks before manual republish:

```bash
cd ~/option_trading
LATEST_REPORT="$(ls -1dt .run/snapshot_parquet/* 2>/dev/null | head -n 1)"
echo "${LATEST_REPORT}"
test -f "${LATEST_REPORT}/coverage_audit.json" && cat "${LATEST_REPORT}/coverage_audit.json"
test -f "${LATEST_REPORT}/build_manifest.json" && cat "${LATEST_REPORT}/build_manifest.json"
```

Only republish manually when:

- `source_missing_count` is `0`
- `buildable_missing_count` is `0`
- `build_manifest.json` describes a publishable local state

Manual republish sequence:

```bash
cd ~/option_trading
source .venv/bin/activate
set -a
source ops/gcp/operator.env
set +a

export REPO_ROOT="${REPO_ROOT:-$HOME/option_trading}"
export PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
export REPORT_ROOT="${REPO_ROOT}/.run/snapshot_parquet/snapshot_publish_$(date -u +%Y%m%dT%H%M%SZ)"

bash ./ops/gcp/publish_snapshot_parquet.sh
```

If those checks fail, stop and rerun the supported wrapper instead.

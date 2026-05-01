# Full System Live Setup Guide

Use this guide when starting from a fresh machine or a fresh GCP project.
It is the single sequential document that gets you from zero to a live trading runtime.

**Audience:** first-time operator or anyone rebuilding from scratch.
**Outcome:** fully running `ml_pure` live trading system on GCP.
**Estimated time:** 3–8 hours depending on data volume and training run duration.

**Read this before any other runbook.**
For detailed procedures on individual phases, the links below point to the dedicated runbooks.

---

## Dependency Order — Never Skip Steps

```
Phase 0  Prerequisites
   ↓
Phase 1  Repo + environment files
   ↓
Phase 2  GCP infrastructure bootstrap
   ↓
Phase 3  Raw market data upload to GCS   (Windows local machine)
   ↓
Phase 4  Snapshot / parquet build         (dedicated Linux build host)
   ↓
Phase 5  Smoke training publish           (validates the pipeline end-to-end)
   ↓
Phase 6  Historical replay validation     (smoke check before live)
   ↓
Phase 7  Production training + publish
   ↓
Phase 8  Kite API auth
   ↓
Phase 9  Live deploy                      (runtime VM start)
   ↓
Phase 10 Verify live runtime
   ↓
Phase 11 Daily operations
   ↓
Phase 12 Cleanup and rollback  (training VM delete, VM stop, teardown, runtime rollback)
```

**Hard rules:**
- Do not go to Phase 5 until Phase 4 (parquet) is complete.
- Do not go to Phase 7 until Phase 5 (smoke publish) succeeds.
- Do not go to Phase 9 until Phase 6 (historical replay) passes.
- Do not go to Phase 9 without valid Kite credentials.

---

## Phase 0: Prerequisites

### 0.1 Required tools on your operator machine

Install all of these before starting. The operator scripts are Bash-based.

| Tool | Min version | Purpose |
|---|---|---|
| `gcloud` CLI | latest | GCP VM control, bucket ops, SSH |
| `terraform` | >= 1.3 | infrastructure provisioning |
| `docker` | >= 24 | GHCR image verification |
| `python3` | >= 3.10 | preflight checks, release manifest, Kite auth |
| `git` | any | repo clone and branch management |
| `bash` | >= 4.4 | all operator scripts |
| `tmux` | any | training session persistence across SSH disconnects |

Use one of:
- Ubuntu 20.04+
- WSL2 on Windows
- GCP Cloud Shell

Do not run the full snapshot/parquet pipeline from Windows Git Bash or Cloud Shell with a small disk.

### 0.2 GCP project

You need an active GCP project with billing enabled. Note your project ID.

Enable required APIs on a brand-new project before Terraform applies:

```bash
gcloud services enable \
  compute.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com \
  serviceusage.googleapis.com \
  iamcredentials.googleapis.com \
  --project "${PROJECT_ID}"
```

Wait ~60 seconds after enabling APIs before running Terraform.

### 0.3 GCP IAM roles for your operator account

Your account needs at minimum:

- `roles/compute.admin`
- `roles/storage.admin`
- `roles/iam.serviceAccountAdmin`
- `roles/iam.serviceAccountUser`
- `roles/resourcemanager.projectIamAdmin`
- `roles/artifactregistry.admin`

Or use `roles/owner` for a simpler setup on a dedicated project.

### 0.4 Authenticate gcloud

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project "${PROJECT_ID}"
gcloud config set compute/zone "${ZONE}"
```

Verify:

```bash
gcloud config list
gcloud projects describe "${PROJECT_ID}"
```

### 0.5 Kite Connect account

You need:

1. A Zerodha trading account.
2. A Kite Connect developer subscription at https://developers.kite.trade/
3. Your **API key** and **API secret** from the Kite developer console.

The API key and secret are used during the live deploy flow (Phase 8) to generate a daily access token.
Store them — you will need them in `operator.env` (Phase 1).

### 0.6 GHCR access (for published images)

If GHCR images are private, create a GitHub personal access token (classic) with `read:packages` scope.
Set `GHCR_USERNAME` and `GHCR_TOKEN` in `operator.env` (see Phase 1).

If images are public, no token is needed.

---

## Phase 1: Repo + Environment Files

### 1.1 Clone the repo

```bash
git clone https://github.com/amitsajwan/option_trading.git
cd option_trading
```

All operator scripts expect to run from the repo root.

### 1.2 Create operator.env

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

**For the `amittrading-493606` project**, run the patch script to replace all template placeholders automatically:

```bash
python3 ops/gcp/patch_operator_env.py
```

This replaces `my-gcp-project`, `my-option-trading-models`, etc. with the real `amittrading-493606` values. Safe to re-run.

For a different project, edit `ops/gcp/operator.env` manually. Required fields:

```bash
# GCP project
PROJECT_ID="your-gcp-project-id"
REGION="asia-south1"
ZONE="asia-south1-a"

# Repo
REPO_CLONE_URL="https://github.com/amitsajwan/option_trading.git"
REPO_REF="main"

# VM names and sizing
RUNTIME_NAME="option-trading-runtime-01"
RUNTIME_MACHINE_TYPE="e2-standard-4"
TRAINING_MACHINE_TYPE="n2-standard-8"

# Image source
IMAGE_SOURCE="ghcr"
GHCR_IMAGE_PREFIX="ghcr.io/amitsajwan"
TAG="latest"

# GCS buckets (names without gs://)
MODEL_BUCKET_NAME="${PROJECT_ID}-option-trading-models"
RUNTIME_CONFIG_BUCKET_NAME="${PROJECT_ID}-option-trading-runtime-config"

# Data sync
DATA_SYNC_SOURCE="gs://your-snapshot-data-bucket/ml_pipeline"

# Kite credentials (used by start_runtime_interactive.sh)
KITE_API_KEY="your-kite-api-key"
KITE_API_SECRET="your-kite-api-secret"

# Training defaults
MODEL_GROUP="banknifty_futures/h15_tp_auto"
PROFILE_ID="openfe_v9_dual"
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.default.json"
```

You can also run the interactive writer instead:

```bash
bash ./ops/gcp/bootstrap_runtime_interactive.sh
```

That prompts for each value and writes the file for you.

### 1.3 Create .env.compose

```bash
cp .env.compose.example .env.compose
```

`operator.env` drives GCP infra. `.env.compose` drives the Docker runtime on the VM.
Do not publish `.env.compose` to GCS in this phase — it is still a template.

**Key relationship:**
- `operator.env` → controls bootstrap + Terraform variables + training inputs
- `.env.compose` → controls what runs inside Docker containers on the runtime VM
- `ops/gcp/publish_runtime_config.sh` → uploads `.env.compose` and referenced artifacts to GCS
- VM startup script → downloads the config bundle from GCS and starts Compose

Update `INSTRUMENT_SYMBOL` in `.env.compose` to the current active futures contract before deploying:

```bash
# Example for current expiry — update this on every contract rollover
INSTRUMENT_SYMBOL=BANKNIFTY26MAYFUT
```

---

## Phase 2: GCP Infrastructure Bootstrap

### 2.1 Run the bootstrap

Use the lifecycle menu:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 1 (Bootstrap infra)
```

Or directly:

```bash
RUN_RUNTIME_CONFIG_SYNC=0 bash ./ops/gcp/from_scratch_bootstrap.sh
```

Use `RUN_RUNTIME_CONFIG_SYNC=0` until `.env.compose` has real runtime values (after Phase 8).

This does:
1. Writes `infra/gcp/terraform.tfvars` from `operator.env`
2. Runs `terraform init` + `terraform apply`
3. Creates the runtime VM, training instance template, GCS buckets, static IP, firewall rules, and service accounts
4. Optionally builds and pushes runtime images to Artifact Registry

### 2.2 Verify infrastructure

```bash
source ops/gcp/operator.env

cd infra/gcp
terraform output
cd -

gcloud compute instances describe "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" --format="value(status)"

gcloud storage ls "gs://${MODEL_BUCKET_NAME}"
gcloud storage ls "gs://${RUNTIME_CONFIG_BUCKET_NAME}"
```

You should see:
- Terraform outputs succeed without error
- Runtime VM exists (status `TERMINATED` or `RUNNING`)
- Both GCS buckets exist

---

## Phase 3: Raw Market Data Upload

**Run from Windows or a machine with access to your local raw data archive.**

Upload the raw BankNifty data archive to the GCS snapshot data bucket:

```bash
# From Windows PowerShell or local machine with gcloud
bash ./ops/gcp/publish_raw_market_data.sh
```

The wrapper expects your local archive to be organized as:

```
LOCAL_RAW_ARCHIVE_ROOT/
  banknifty_fut/
  banknifty_options/
  banknifty_spot/
  VIX/
```

Set `RAW_ARCHIVE_BUCKET_URL` in `operator.env` before running:

```bash
RAW_ARCHIVE_BUCKET_URL="gs://your-snapshot-data-bucket/banknifty_data"
```

Verify:

```bash
gcloud storage ls "${RAW_ARCHIVE_BUCKET_URL}/"
```

You should see the four data subdirectories.

---

## Phase 4: Snapshot / Parquet Build

**Run from a dedicated Linux build VM or a large-disk Linux host.** Not from Cloud Shell.

Disk requirements: 150 GB+ free. Use a VM with 8–16 vCPU and 16 GB+ RAM.

### 4.1 Set snapshot bucket variables in operator.env

```bash
SNAPSHOT_DATA_BUCKET_NAME="your-snapshot-data-bucket"
RAW_ARCHIVE_BUCKET_URL="gs://your-snapshot-data-bucket/banknifty_data"
SNAPSHOT_PARQUET_BUCKET_URL="gs://your-snapshot-data-bucket/parquet_data"
```

### 4.2 Run the snapshot/parquet pipeline

Start inside `tmux` before running. This can take 30–90 minutes.

```bash
tmux new -s snapshot_build
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh
```

If the session disconnects, reattach and re-run the same command. The wrapper is resumable.

### 4.3 Verify parquet outputs

```bash
gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL}/"
```

You must see all of these dataset prefixes before proceeding:

- `snapshots/`
- `snapshots_ml_flat/`
- `stage1_entry_view/`
- `stage2_direction_view/`
- `stage3_recipe_view/`
- `reports/`

If any are missing, re-run the snapshot pipeline.

See [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) for full parquet build detail.

---

## Phase 5: Smoke Training Publish

Before running production research, validate the full training pipeline with a smoke run.

### 5.1 Create a training VM

```bash
bash ./ops/gcp/create_training_vm.sh
```

Wait for it to reach `RUNNING`:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" --format="value(status)"
```

### 5.2 SSH to the training VM and verify startup

```bash
gcloud compute ssh "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

On the VM:

```bash
cd /opt/option_trading
git rev-parse --short HEAD
find .data/ml_pipeline/parquet_data -maxdepth 2 -type d | sort
```

You should see the repo checkout and parquet datasets synced from `DATA_SYNC_SOURCE`.

If parquet is missing, sync it manually:

```bash
gcloud storage rsync "${DATA_SYNC_SOURCE}" .data/ml_pipeline --recursive
```

### 5.3 Run the interactive training launcher

```bash
bash ./ops/gcp/start_training_interactive.sh
# Choose: 2 (test_quick) for the smoke run
```

The launcher auto-starts inside a `tmux` session. Reconnect with the printed session name if SSH drops.

For the smoke run use `test_quick` mode. It runs the full staged pipeline in a research lane without publishing to the production model group.

### 5.4 Verify smoke training output

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/training_launches \
  -name "training.log" | sort | tail -3
```

Look for `release_status` in the training summary printed at the end.
A HOLD on `test_quick` is expected — this mode is not intended to publish.

Delete the training VM when done:

```bash
bash ./ops/gcp/delete_training_vm.sh
```

See [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md) for full training detail.

---

## Phase 6: Historical Replay Validation

Replay validates that the strategy pipeline produces expected outputs on historical data before live deployment.

### 6.1 Start the historical replay interactive helper

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 3 (Historical replay)
```

The helper prompts for:
- Target VM name and remote repo checkout path
- GHCR image prefix and tag
- Snapshot parquet bucket URL
- Replay start and end dates

It then:
1. Syncs parquet onto the VM
2. Starts historical consumers
3. Runs a one-shot replay job

### 6.2 Verify replay outputs

After replay completes:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "cd /opt/option_trading && \
  sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  logs --tail 100 strategy_app_historical"
```

You should see strategy decisions produced for each replayed snapshot.

See [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md#2-historical) for full historical replay detail.

---

## Phase 7: Production Training + Publish

When the smoke run and historical replay both succeed, run production training.

### 7.1 Create a training VM

```bash
bash ./ops/gcp/create_training_vm.sh
```

### 7.2 Run the training launcher

```bash
bash ./ops/gcp/start_training_interactive.sh
# Choose: 1 (publish_full)
```

This runs Stage 1 + Stage 2 + Stage 3 with strict publish gates.
A successful run produces:

- Published model artifacts in the model bucket
- `release/current_runtime_release.json` in the runtime-config bucket
- `release/current_ml_pure_runtime.env` in the runtime-config bucket
- Local cache at `.run/gcp_release/current_runtime_release.json`

If the run returns `HOLD`, do not deploy to live. Investigate the blocking reasons before retrying.

Research decision order when baseline publish holds:

1. `deep_search` — identifies whether Stage 1 or Stage 2 is the bottleneck
2. `stage1_hpo` or `stage1_diag` — if Stage 1 is blocking
3. `stage2_hpo` — if Stage 2 is blocking after deep search
4. `stage2_edge` — if stage2 HPO still holds with similar metrics

### 7.3 Delete the training VM

```bash
bash ./ops/gcp/delete_training_vm.sh
```

---

## Phase 8: Kite API Authentication

The live runtime needs a daily access token from Kite Connect.
Tokens expire at midnight IST. You must refresh the token before each trading day.

### 8.1 Where to get API key and secret

1. Log in at https://developers.kite.trade/
2. Create or open your app
3. Copy the **API key** and **API secret**

### 8.2 Set credentials in operator.env

```bash
KITE_API_KEY="your-api-key"
KITE_API_SECRET="your-api-secret"
```

The live deploy helper reads these to run the browser auth flow.

### 8.3 Run browser auth

```bash
python3 -m ingestion_app.kite_auth --force
```

This:
1. Generates a Kite login URL and opens it in your browser
2. Waits for you to complete the login in the browser
3. Captures the `request_token` from the redirect
4. Exchanges it for an `access_token`
5. Writes `ingestion_app/credentials.json`

`credentials.json` contains `api_key` and `access_token`. Keep this file local — do not commit it.

The live deploy helper (`start_runtime_interactive.sh`) will prompt to run this auth, check credentials state, and sync `KITE_API_KEY` + `KITE_ACCESS_TOKEN` into `.env.compose` before publishing.

---

## Phase 9: Live Deploy

### 9.1 Run the live deploy flow

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 2 (Start/restart runtime)
```

The interactive helper:

1. Downloads the current approved release from the runtime-config bucket
2. Shows: `run_id`, `model_group`, `app_image_tag`
3. Applies the ml_pure release handoff into `.env.compose`
4. Prompts for Kite browser auth or confirms existing credentials are valid
5. Runs shared live preflight (release manifest, runtime bundle, GHCR images, Kite state)
6. Publishes the runtime config bundle to GCS
7. Starts or restarts the runtime VM

Preflight blocks if any of these are missing or wrong:
- Release manifest not `PUBLISHED`
- `STRATEGY_ROLLOUT_STAGE` not `capped_live`
- `STRATEGY_POSITION_SIZE_MULTIPLIER` > 0.25
- `STRATEGY_ML_RUNTIME_GUARD_FILE` missing or not pointing to a valid guard
- Kite credentials missing or stale
- GHCR images not found for the release tag

If preflight blocks, fix the blocker and re-run.

### 9.2 Runtime guard file

The live runtime requires a guard file at the path set in `STRATEGY_ML_RUNTIME_GUARD_FILE` (default `.run/ml_runtime_guard_live.json`).

The guard file must assert:

```json
{
  "approved_for_runtime": true,
  "offline_strict_positive_passed": true,
  "paper_days_observed": 10,
  "shadow_days_observed": 10
}
```

The deploy helper will offer to create a smoke guard if the file is missing.
Do not deploy with a smoke guard on a model that has not completed paper + shadow observation.

---

## Phase 10: Verify Live Runtime

After the VM starts, verify from the operator machine:

### Startup log

```bash
gcloud compute ssh "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "sudo tail -n 200 /var/log/option-trading-runtime-startup.log"
```

### Compose service status

```bash
gcloud compute ssh "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "cd /opt/option_trading && \
  sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml ps"
```

### Strategy app log

```bash
gcloud compute ssh "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "cd /opt/option_trading && \
  sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  logs --tail 120 strategy_app"
```

### Snapshot feed

```bash
gcloud compute ssh "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "tail -n 10 /opt/option_trading/.run/snapshot_app/events.jsonl"
```

### Signal output

```bash
gcloud compute ssh "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "tail -n 10 /opt/option_trading/.run/strategy_app/signals.jsonl"
```

### Dashboard health (if enabled)

```bash
gcloud compute ssh "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command "curl -fsS http://127.0.0.1:8008/api/health"
```

**You should see:**
- All services are `Up` in `docker compose ps`
- `strategy_app` logs show `engine=ml_pure`
- Snapshots are advancing with current timestamps
- Strategy signals are produced after each snapshot during market hours
- Dashboard returns HTTP 200

---

## Phase 11: Daily Operations

The live runtime is always-on. Daily tasks are:

### Before market open (by 09:00 IST)

1. **Refresh Kite access token** — tokens expire at midnight IST:

```bash
python3 -m ingestion_app.kite_auth --force
```

2. **Redeploy with fresh credentials:**

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 2 (Start/restart runtime)
# Accept the current approved release
# The helper will sync fresh Kite credentials into .env.compose
```

3. **Verify the runtime is healthy** (use Phase 10 commands above)

4. **Check `INSTRUMENT_SYMBOL`** — update `.env.compose` on contract rollover expiry dates and redeploy

### End of day / Cost saving

Stop the runtime VM when market is closed and you do not need it:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 4 (Stop runtime VM)
```

Or directly:

```bash
bash ./ops/gcp/stop_runtime.sh
```

`stop_runtime.sh` SSHes to the VM and runs `docker compose down` on all profiles before issuing the GCP stop command. This drains containers gracefully (MongoDB flush, strategy_app clean exit). If SSH is unreachable, it falls back to a hard VM stop automatically.

To bypass graceful stop (emergency or SSH unreachable):

```bash
SKIP_GRACEFUL_STOP=1 bash ./ops/gcp/stop_runtime.sh
```

The model bucket and runtime-config bucket are not affected. Restart next morning with menu item 2.

**VM startup time depends on `IMAGE_SOURCE`:**

| `IMAGE_SOURCE` | What happens on restart | Typical time |
|---|---|---|
| `ghcr` (recommended) | pulls pre-built images from GHCR | ~1-2 min |
| `local_build` | rebuilds all Docker images from source | ~5-7 min |

If your `.env.compose` has `IMAGE_SOURCE=local_build`, switch it to `IMAGE_SOURCE=ghcr` for daily stop/start cycles. Keep `local_build` only when testing uncommitted code changes.

**Historical and eval containers do not auto-start after VM restart.**

The startup script only launches the 7 core services (`redis`, `mongo`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_app`, `strategy_persistence_app`) plus `dashboard` if enabled. If you were running historical replay or strategy eval services before stopping the VM, restart them manually:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" \
  --command 'cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical --profile strategy_eval up -d 2>&1 | tail -10'
```

### When a new model is ready

After a production training publish completes:

1. The current approved release artifacts in the runtime-config bucket update automatically
2. Run the live deploy flow (menu item 2) — it will auto-load the new release
3. Verify strategy_app starts with the new `ML_PURE_RUN_ID`

### On contract rollover

1. Update `INSTRUMENT_SYMBOL` in `.env.compose` to the new expiry
2. Re-run the live deploy flow — it publishes the updated config and restarts the VM

---

## Quick Reference: Script Entry Points

| Goal | Command |
|---|---|
| Full lifecycle menu | `bash ./ops/gcp/runtime_lifecycle_interactive.sh` |
| Write operator.env interactively | `bash ./ops/gcp/bootstrap_runtime_interactive.sh` |
| Bootstrap infra only | `bash ./ops/gcp/from_scratch_bootstrap.sh` |
| Deploy / restart live runtime | Menu item 2 or `bash ./ops/gcp/start_runtime_interactive.sh` |
| Historical replay | Menu item 3 or `bash ./ops/gcp/start_historical_interactive.sh` |
| Training (interactive) | Menu item 6 or `bash ./ops/gcp/start_training_interactive.sh` |
| Snapshot / parquet build | `bash ./ops/gcp/run_snapshot_parquet_pipeline.sh` |
| Upload raw data (local/Windows) | `bash ./ops/gcp/publish_raw_market_data.sh` |
| Publish runtime config to GCS | `bash ./ops/gcp/publish_runtime_config.sh` |
| Create training VM | `bash ./ops/gcp/create_training_vm.sh` |
| Delete training VM | `bash ./ops/gcp/delete_training_vm.sh` |
| Stop runtime VM (graceful drain + stop) | `bash ./ops/gcp/stop_runtime.sh` |
| Stop runtime VM (skip container drain) | `SKIP_GRACEFUL_STOP=1 bash ./ops/gcp/stop_runtime.sh` |
| Destroy infra, keep data | `bash ./ops/gcp/destroy_infra_preserve_data.sh` |
| Kite browser auth | `python3 -m ingestion_app.kite_auth --force` |
| Run infra preflight check | `python3 ops/gcp/operator_preflight.py --mode infra --repo-root . --operator-env-file ops/gcp/operator.env` |
| Run live preflight check | `python3 ops/gcp/operator_preflight.py --mode live --repo-root . --env-file .env.compose --release-manifest-path <path> --image-source ghcr --ghcr-image-prefix ghcr.io/amitsajwan --credentials-path ingestion_app/credentials.json` |

---

## Phase 12: Cleanup and Rollback

Use the right cleanup mode for the situation. They differ in what is preserved and what is destroyed.

---

### 12.1 Delete a Training VM After Use

Always delete the disposable training VM after each training run to stop compute cost:

```bash
bash ./ops/gcp/delete_training_vm.sh
```

Verify it is gone:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" --zone "${ZONE}" --format="value(status)"
```

You should see: `ERROR: ... not found`

The model bucket, runtime-config bucket, and runtime VM are not affected.

---

### 12.2 End-of-Day: Stop the Runtime VM (Cheap Idle)

Stop compute cost at end of day without removing any persistent resources:

```bash
bash ./ops/gcp/stop_runtime.sh
```

Or through the lifecycle menu:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 4 (Stop runtime VM)
```

`stop_runtime.sh` SSHes to the VM first and runs `docker compose down --timeout 30` across all profiles before issuing the GCP stop. If SSH fails it falls back to a hard stop. To force a hard stop without the SSH drain step:

```bash
SKIP_GRACEFUL_STOP=1 bash ./ops/gcp/stop_runtime.sh
```

What is preserved:
- Runtime VM definition and static IP
- Firewall rules and IAM
- Model bucket and runtime-config bucket
- GHCR-published images

Restart the next morning with menu item 2 (Start/restart runtime).

### 12.2a Manual recreate — when terraform is unavailable or tfstate is lost

If terraform is not installed (e.g. Windows operator) or the local `.tfstate` was lost, recreate just the runtime compute instance directly with gcloud. All persistent resources (service account, static IP, firewall, GCS buckets) survive a `gcloud instances delete` and are reused.

**Step 1 — Render the startup script** (PowerShell):

```powershell
$template = Get-Content "infra/gcp/templates/runtime-startup.sh.tftpl" -Raw
$rendered = $template `
  -replace '\$\$', '$' `
  -replace '\$\{runtime_config_sync_source\}', 'gs://<project>-option-trading-runtime-config/runtime' `
  -replace '\$\{published_models_sync_source\}', 'gs://<project>-option-trading-models/published_models' `
  -replace '\$\{data_sync_source\}',             'gs://<project>-option-trading-snapshots/ml_pipeline' `
  -replace '\$\{project_id\}',                   '<project>' `
  -replace '\$\{repo_clone_url\}',               'https://github.com/<org>/option_trading.git' `
  -replace '\$\{repo_ref\}',                     'main' `
  -replace '\$\{runtime_os_user\}',              'ubuntu' `
  -replace '\$\{app_image_tag\}',                'latest' `
  -replace '\$\{dashboard_port\}',               '8008' `
  -replace '\$\{enable_dashboard_profile\}',     'true'
$rendered | Set-Content "$env:TEMP\runtime-startup-rendered.sh" -Encoding UTF8
```

> **Key:** Replace ALL `$$` → `$` first (Terraform escape convention), then substitute `${var}` placeholders.

**Step 2 — Recreate the VM**:

```powershell
gcloud compute instances create option-trading-runtime-01 `
  --project <project> --zone asia-south1-b `
  --machine-type e2-standard-4 `
  --boot-disk-size 100GB --boot-disk-type pd-balanced `
  --image-family ubuntu-2204-lts --image-project ubuntu-os-cloud `
  --service-account option-trading-runtime@<project>.iam.gserviceaccount.com `
  --scopes cloud-platform `
  --tags option-trading-runtime `
  --address option-trading-runtime-01-ip `
  --metadata-from-file startup-script="$env:TEMP\runtime-startup-rendered.sh"
```

The startup script pulls `.env.compose`, Kite credentials, published models, and parquet data from GCS automatically. Allow ~10 min for first boot (data sync + image build). To avoid this going forward, enable the GCS terraform backend in `infra/gcp/versions.tf` — see the comment block at the top of that file.

---

### 12.3 Preserve-Data Teardown (Longer Pause)

Remove cost-bearing compute and networking while keeping all deployable data in GCS:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# Choose: 5 (Destroy infra, preserve data)
```

Or directly:

```bash
AUTO_APPROVE=1 bash ./ops/gcp/destroy_infra_preserve_data.sh
```

What is preserved:
- Model bucket
- Runtime-config bucket
- Snapshot data bucket (if created)
- GHCR-published images

What is destroyed:
- Runtime VM and training instance template
- Static IP
- Firewall rules
- Runtime and training service accounts
- Terraform-managed IAM grants

To come back: re-run infra bootstrap. The existing buckets and images are picked up automatically.

```bash
RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 bash ./ops/gcp/from_scratch_bootstrap.sh
```

---

### 12.4 Full Wipe (Remove Everything)

Use only if you want to remove everything Terraform manages, including buckets:

```bash
cd infra/gcp
terraform destroy
```

Warning: bucket deletion fails if the bucket is not empty. Empty buckets first or remove the bucket resource from state if you want to keep the data.

---

### 12.5 Runtime Rollback (Bad Deploy)

If a live deploy produces bad runtime behavior:

1. Back up the current `.env.compose` before each deploy — keep a copy outside the repo
2. Restore the previous `.env.compose`
3. Republish the runtime config:

```bash
bash ./ops/gcp/publish_runtime_config.sh
```

4. Restart the VM:

```bash
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

If the problem is the image tag, change `APP_IMAGE_TAG` in `.env.compose` back to the known-good tag and repeat steps 3–4.

If the problem is the ML model artifacts, revert `ML_PURE_RUN_ID` and `ML_PURE_MODEL_GROUP` in `.env.compose` to the previous published run and repeat steps 3–4.

---

### 12.6 Runtime Guard Revocation

If you need to halt live sizing immediately:

1. Edit `.run/ml_runtime_guard_live.json` — set `"approved_for_runtime": false`
2. Republish and restart:

```bash
bash ./ops/gcp/publish_runtime_config.sh
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

The runtime will start in `capped_live` with a failed guard, which blocks position sizing at startup.

See [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md) for the full cleanup reference.

---

## Key File Map

| File | Purpose |
|---|---|
| `ops/gcp/operator.env` | GCP project, bucket names, Kite credentials, training defaults |
| `ops/gcp/operator.env.example` | Template — copy to `operator.env` and fill in |
| `.env.compose` | Docker runtime environment for all containers on the VM |
| `.env.compose.example` | Template — copy to `.env.compose` |
| `infra/gcp/terraform.tfvars` | Generated by bootstrap from `operator.env` |
| `ingestion_app/credentials.json` | Kite API access token — refreshed daily |
| `.run/ml_runtime_guard_live.json` | Live runtime guard — must be approved before `capped_live` |
| `.run/gcp_release/current_runtime_release.json` | Current approved release manifest cache |

---

## Linked Runbooks

| Runbook | Covers |
|---|---|
| [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) | Full parquet build on a dedicated build host |
| [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md) | Staged training, research lanes, publish, runtime handoff |
| [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) | Infra + live + historical, deep-dive command reference |
| [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md) | Stop spend, teardown modes, runtime rollback |
| [DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md](DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md) | Local deterministic replay for research |

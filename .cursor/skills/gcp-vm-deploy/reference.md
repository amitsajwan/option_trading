# GCP VM Deploy — Reference

> **⚠️ 2026-06-09: project migrated to `amit-trading`.** The old project
> **`algo-trading-496203` is DEAD (billing suspended) — do NOT use it.** The full
> stack (runtime VM, ML template, models + snapshots + runtime-config buckets,
> 2020-2024 training parquet) is on `amit-trading`. Verified live.

## Environment constants (`amit-trading`)

| Setting | Value |
|---------|-------|
| Project | `amit-trading` |
| Zone | `asia-south1-b` |
| Runtime VM | `option-trading-runtime-01` (RUNNING) |
| **Runtime checkout** | **`/opt/option_trading`** (verified — NOT `~/option-trading-runtime`) |
| ML VM | `option-trading-ml-01` — create from the template below (not pre-running) |
| ML training template | `option-trading-training-template-20260603054605306500000001` (n2-standard-8, Ubuntu 22.04, 250GB) |
| ML checkout | detect on first use (`/opt/option_trading` or `~/option-trading-ml`) |

## GCS buckets (`amit-trading`)

| Purpose | URL |
|---------|-----|
| Published models | `gs://amit-trading-option-trading-models/published_models` |
| Runtime config bundle | `gs://amit-trading-option-trading-runtime-config/runtime` |
| Parquet / training data | `gs://amit-trading-option-trading-snapshots/ml_pipeline/parquet_data` (2020→2024) |
| Parquet data (local on VM) | `<checkout>/.data/ml_pipeline/parquet_data` |

## Create the ML VM (from the migrated template)

```bash
gcloud compute instances create option-trading-ml-01 \
  --source-instance-template=option-trading-training-template-20260603054605306500000001 \
  --project=amit-trading --zone=asia-south1-b
```

Then sync training data + run:

```bash
gcloud compute ssh option-trading-ml-01 --project=amit-trading --zone=asia-south1-b --command "
  cd /opt/option_trading 2>/dev/null || cd ~/option-trading-ml
  git pull origin feat/intelligent-brain
  gcloud storage rsync -r gs://amit-trading-option-trading-snapshots/ml_pipeline/parquet_data .data/ml_pipeline/parquet_data
  PLAYGROUND_MODE=hpo bash ops/gcp/run_ml_playground_overnight_vm.sh start
"
```

Sync models to runtime VM:

```bash
gcloud compute ssh option-trading-runtime-01 --project=amit-trading --zone=asia-south1-b --command "
  mkdir -p /opt/option_trading/ml_pipeline_2/artifacts/published_models
  gcloud storage rsync gs://amit-trading-option-trading-models/published_models \
    /opt/option_trading/ml_pipeline_2/artifacts/published_models --recursive
"
```

## `ops/gcp/operator.env` template

```bash
PROJECT_ID="amit-trading"
REGION="asia-south1"
ZONE="asia-south1-b"
RUNTIME_NAME="option-trading-runtime-01"
MODEL_BUCKET_NAME="amit-trading-option-trading-models"
RUNTIME_CONFIG_BUCKET_NAME="amit-trading-option-trading-runtime-config"
MODEL_BUCKET_URL="gs://amit-trading-option-trading-models/published_models"
RUNTIME_CONFIG_BUCKET_URL="gs://amit-trading-option-trading-runtime-config/runtime"
DATA_SYNC_SOURCE="gs://amit-trading-option-trading-snapshots/ml_pipeline"
SNAPSHOT_PARQUET_BUCKET_URL="gs://amit-trading-option-trading-snapshots/ml_pipeline/parquet_data"
```

## Checkout paths

The **runtime VM checkout is `/opt/option_trading`** (verified — this is where
`.env.compose`, `docker-compose.yml`, and `strategy_app/` live; all session ops
used it). If a command fails with "directory not found", detect the actual path:

```bash
gcloud compute ssh <VM> --zone=asia-south1-b --project=amit-trading --command "
  for d in /opt/option_trading ~/option-trading-ml ~/option-trading-runtime ~/option_trading; do
    [ -f \"\$d/docker-compose.yml\" ] && echo \"FOUND: \$d\"
  done
"
```

## Key operator scripts

| Script | Use |
|--------|-----|
| `ops/gcp/run_ml_playground_overnight_vm.sh` | One-command overnight ML HPO (entry + direction) |
| `ops/gcp/runtime_lifecycle_interactive.sh` | Main menu: infra / live / historical |
| `ops/gcp/start_runtime_interactive.sh` | Live deploy with Kite + release manifest |
| `ops/gcp/start_training_interactive.sh` | Training VM workflows |
| `ops/gcp/publish_runtime_config.sh` | Publish `.env.compose` bundle to GCS |
| `ops/gcp/stop_runtime.sh` | Stop runtime VM safely |

> Some scripts still hard-code `algo-trading-496203` in env vars / `PROJECT`
> defaults (e.g. `operator.env`, `backup_to_local.sh`, token-refresh units,
> `infra/gcp/terraform.tfvars`). **Set `PROJECT_ID=amit-trading` / pass
> `--project=amit-trading` explicitly** until those are swept. Do NOT let a
> default send you back to the dead project.

## Runtime verification commands

```bash
# startup / service logs
gcloud compute ssh option-trading-runtime-01 --project=amit-trading --zone=asia-south1-b --command "
  cd /opt/option_trading &&
  sudo docker compose --env-file .env.compose logs --tail 120 strategy_app
"
```

## ML VM notes

- Training data: `gs://amit-trading-option-trading-snapshots/ml_pipeline/parquet_data` (2020→2024-10; rsync to `<checkout>/.data/ml_pipeline/parquet_data`).
- Long jobs: use `tmux` or `nohup`; log to `ml_pipeline_2/artifacts/research/<run_id>/`.
- The ML VM is **not always running** — create it from the template, run the job, then stop/delete it to save cost.

# GCP VM Deploy — Reference

## Environment constants (`algo-trading-496203`)

| Setting | Value |
|---------|-------|
| Project | `algo-trading-496203` |
| Zone | `asia-south1-b` |
| ML VM | `option-trading-ml-01` |
| ML checkout | `~/option-trading-ml` |
| Runtime VM | `option-trading-runtime-01` |
| Runtime checkout | `~/option-trading-runtime` |

## GCS buckets (from `infra/gcp/terraform.tfvars`)

| Purpose | URL |
|---------|-----|
| Published models | `gs://algo-trading-496203-option-trading-models/published_models` |
| Runtime config bundle | `gs://algo-trading-496203-option-trading-runtime-config/runtime` |
| Parquet / training data parent | `gs://algo-trading-496203-option-trading-snapshots/ml_pipeline` |
| Parquet data (local on VM) | `<checkout>/.data/ml_pipeline/parquet_data` |

Sync models to runtime VM:

```bash
gcloud compute ssh option-trading-runtime-01 --project=algo-trading-496203 --zone=asia-south1-b --command "
  mkdir -p ~/option-trading-runtime/ml_pipeline_2/artifacts/published_models
  gcloud storage rsync gs://algo-trading-496203-option-trading-models/published_models \
    ~/option-trading-runtime/ml_pipeline_2/artifacts/published_models --recursive
"
```

## `ops/gcp/operator.env` template

Copy and set for this project:

```bash
PROJECT_ID="algo-trading-496203"
REGION="asia-south1"
ZONE="asia-south1-b"
RUNTIME_NAME="option-trading-runtime-01"
MODEL_BUCKET_NAME="algo-trading-496203-option-trading-models"
RUNTIME_CONFIG_BUCKET_NAME="algo-trading-496203-option-trading-runtime-config"
MODEL_BUCKET_URL="gs://algo-trading-496203-option-trading-models/published_models"
RUNTIME_CONFIG_BUCKET_URL="gs://algo-trading-496203-option-trading-runtime-config/runtime"
DATA_SYNC_SOURCE="gs://algo-trading-496203-option-trading-snapshots/ml_pipeline"
SNAPSHOT_PARQUET_BUCKET_URL="gs://algo-trading-496203-option-trading-snapshots/ml_pipeline/parquet_data"
```

## Legacy checkout paths

Older docs and startup scripts may use:

- `/opt/option_trading`
- `~/option_trading`
- `~/option_trading_repo`

The **canonical paths for this project** are `~/option-trading-ml` and `~/option-trading-runtime`. If a command fails with "directory not found", detect the actual path:

```bash
gcloud compute ssh <VM> --zone=asia-south1-b --project=algo-trading-496203 --command "
  for d in ~/option-trading-ml ~/option-trading-runtime ~/option_trading /opt/option_trading; do
    [ -f \"\$d/docker-compose.yml\" ] && echo \"FOUND: \$d\"
  done
"
```

## Key operator scripts

| Script | Use |
|--------|-----|
| `ops/gcp/runtime_lifecycle_interactive.sh` | Main menu: infra / live / historical |
| `ops/gcp/start_runtime_interactive.sh` | Live deploy with Kite + release manifest |
| `ops/gcp/start_training_interactive.sh` | Training VM workflows |
| `ops/gcp/publish_runtime_config.sh` | Publish `.env.compose` bundle to GCS |
| `ops/gcp/run_historical_replay_shell.sh` | Non-interactive historical replay |
| `ops/gcp/force_deploy_research_run.sh` | Force live deploy of a research run |
| `ops/gcp/stop_runtime.sh` | Stop runtime VM safely |

## Runtime verification commands

Startup log:

```bash
gcloud compute ssh option-trading-runtime-01 --project=algo-trading-496203 --zone=asia-south1-b \
  --command "sudo tail -n 200 /var/log/option-trading-runtime-startup.log"
```

Service logs:

```bash
REPO=~/option-trading-runtime
gcloud compute ssh option-trading-runtime-01 --project=algo-trading-496203 --zone=asia-south1-b --command "
  cd ${REPO} &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml logs --tail 120 strategy_app
"
```

## ML VM notes

- Data often lives under `~/option-trading-ml/.data/ml_pipeline/parquet_data` or was synced from `gs://algo-trading-496203-option-trading-snapshots/ml_pipeline`.
- Long jobs: use `tmux` or `nohup` and log to `ml_pipeline_2/artifacts/research/<run_id>/`.
- Pull research artifacts down with `ops/gcp/backup_to_local.sh` (update `PROJECT` in that script if it still references an old project id).

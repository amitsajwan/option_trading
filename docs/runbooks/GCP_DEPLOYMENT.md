# Live Runtime Runbook

Use this runbook to deploy the live runtime on GCP, publish runtime config, restart the runtime VM, validate the live stack, and roll back if needed.

This workflow is self-contained. It includes the GCP setup it needs.

## Fast Path (Interactive)

Use the supported operator flow:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Recommended order:

1. choose `1` once per environment to write `ops/gcp/operator.env` and optionally bootstrap infra
2. choose `2` for each runtime deploy or restart
3. choose `3` at end of day to stop compute

Direct entrypoints:

```bash
bash ./ops/gcp/bootstrap_runtime_interactive.sh
bash ./ops/gcp/start_runtime_interactive.sh
```

The runtime deploy helper prompts for:

- project, region, zone, and runtime VM name
- runtime-config bucket URL
- `GHCR_IMAGE_PREFIX` and image tag
- `ML_PURE_RUN_ID` and `ML_PURE_MODEL_GROUP`

`start_runtime_interactive.sh` also supports:

- optional Kite browser auth (`python -m ingestion_app.kite_auth --force`)
- prompts for `KITE_API_KEY` and hidden `KITE_API_SECRET` during auth when not already exported
- automatic sync of `KITE_API_KEY` and `KITE_ACCESS_TOKEN` from `ingestion_app/credentials.json` into `.env.compose`
- automatic `INGESTION_COLLECTORS_ENABLED=1`
- prompt-driven install of Kite auth dependencies on the operator host when missing
- GHCR image existence preflight when Docker is available on the operator host
- prompt-driven `start`, `restart`, or `skip` VM action after runtime config publish

Bootstrap note:

- if `.env.compose` was auto-created from `.env.compose.example`, bootstrap skips runtime-config publish automatically and prints the follow-up command

## What This Produces

- runtime images in GHCR
- runtime config bundle in the runtime-config bucket
- a live runtime VM running the Compose stack

## Step 1: Prepare Shared GCP Resources

If the runtime VM and buckets do not exist yet:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

You need at least these values in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `RUNTIME_NAME`
- `TAG`
- `GHCR_IMAGE_PREFIX`
- `MODEL_BUCKET_NAME`
- `RUNTIME_CONFIG_BUCKET_NAME`

`MODEL_BUCKET_URL` and `RUNTIME_CONFIG_BUCKET_URL` are optional. Current bootstrap derives them as:

- `MODEL_BUCKET_URL=gs://<MODEL_BUCKET_NAME>/published_models`
- `RUNTIME_CONFIG_BUCKET_URL=gs://<RUNTIME_CONFIG_BUCKET_NAME>/runtime`

`REPOSITORY` still exists in `operator.env` and Terraform variables for infra compatibility, but the live runtime path now uses GHCR images rather than Artifact Registry images.

Verify:

```bash
cd infra/gcp
terraform output
gcloud compute instances describe "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --format="value(status)"
gcloud storage ls "gs://${MODEL_BUCKET_NAME}"
gcloud storage ls "gs://${RUNTIME_CONFIG_BUCKET_NAME}"
```

Look for:

- Terraform outputs succeed
- runtime VM exists
- model and runtime-config buckets exist

## Step 2: Select GHCR Image Tag

Live runtime images are expected in GHCR.

Set these values in `ops/gcp/operator.env` and `.env.compose`:

```env
GHCR_IMAGE_PREFIX=ghcr.io/amitsajwan
APP_IMAGE_TAG=latest
```

Minimum image set for the full runtime stack:

- `ingestion_app`
- `snapshot_app`
- `persistence_app`
- `strategy_app`
- `market_data_dashboard`
- `strategy_eval_orchestrator`
- `strategy_eval_ui`

Verify the pinned tag exists for all required images:

```bash
export GHCR_IMAGE_PREFIX APP_IMAGE_TAG
for svc in ingestion_app snapshot_app persistence_app strategy_app market_data_dashboard strategy_eval_orchestrator strategy_eval_ui; do
  docker manifest inspect "${GHCR_IMAGE_PREFIX}/${svc}:${APP_IMAGE_TAG}" >/dev/null 2>&1 \
    && echo "ok: ${svc}:${APP_IMAGE_TAG}" \
    || echo "missing: ${svc}:${APP_IMAGE_TAG}"
done
```

Look for:

- `ok:` for every required service
- no `missing:` lines before runtime restart

If you are using `start_runtime_interactive.sh`, this check is already built in when Docker is available.

## Step 3: Prepare Runtime Config

If training just produced a publishable release handoff:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_id>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

`apply_ml_pure_release.sh` only writes the staged handoff keys:

- `STRATEGY_ENGINE`
- `ML_PURE_RUN_ID`
- `ML_PURE_MODEL_GROUP`

It does not make the repo live-ready by itself. Before publishing runtime config, `.env.compose` must also contain the live rollout and monitoring prerequisites that the runtime and dashboard expect.

Supported live settings:

```env
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=<published_run_id>
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
STRATEGY_ROLLOUT_STAGE=capped_live
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE=.run/ml_runtime_guard_live.json
ML_PURE_THRESHOLD_REPORT=ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/threshold_report.json
ML_PURE_TRAINING_SUMMARY_PATH=ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/training_report.json
```

Notes:

- `publish_runtime_config.sh` requires the capped-live fields and the repo-relative guard file before it will publish an `ml_pure` runtime config
- `docker-compose.gcp.yml` requires `GHCR_IMAGE_PREFIX` and `APP_IMAGE_TAG` in `.env.compose`
- `ML_PURE_THRESHOLD_REPORT` is optional for run-id model resolution in `strategy_app`, but it is the supported way to anchor rolling Stage 1 precision monitoring to the deployed threshold report
- `ML_PURE_TRAINING_SUMMARY_PATH` is optional for core trading, but without it the persistence/dashboard regime-drift monitor cannot compare live regime mix against the training baseline
- use repo-relative paths so the runtime VM can sync them through the runtime-config bundle and local checkout layout

Verify:

```bash
grep -E "STRATEGY_ENGINE|ML_PURE_RUN_ID|ML_PURE_MODEL_GROUP|STRATEGY_ROLLOUT_STAGE|STRATEGY_POSITION_SIZE_MULTIPLIER|STRATEGY_ML_RUNTIME_GUARD_FILE|ML_PURE_THRESHOLD_REPORT|ML_PURE_TRAINING_SUMMARY_PATH" .env.compose
grep -E "GHCR_IMAGE_PREFIX|APP_IMAGE_TAG" .env.compose
test -f .run/ml_runtime_guard_live.json && echo guard_ok
```

Look for:

- all required env keys are present
- GHCR image prefix and tag are present
- guard file exists locally

## Step 4: Publish Runtime Config

```bash
export RUNTIME_CONFIG_BUCKET_URL
./ops/gcp/publish_runtime_config.sh
```

Verify:

- command exits successfully
- the helper does not reject `.env.compose`

Also verify the bucket:

```bash
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

Look for:

- `.env.compose`
- `ingestion_app/credentials.json` if used
- `.run/ml_runtime_guard_live.json` when `ml_pure` is enabled
- the repo-relative threshold report path referenced by `ML_PURE_THRESHOLD_REPORT`, if configured
- the repo-relative training summary path referenced by `ML_PURE_TRAINING_SUMMARY_PATH`, if configured

## Step 5: Start Or Restart The Runtime VM

```bash
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

If you are using the interactive deploy flow, this restart is already prompted for and performed by `start_runtime_interactive.sh`.

Verify:

```bash
gcloud compute instances describe "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- `RUNNING`

Notes:

- restarting the VM is the supported rollout path because startup syncs runtime config and published model artifacts, validates runtime bundle inputs, and then pulls and starts the pinned-tag images
- the runtime startup stack includes `redis`, `mongo`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_app`, and `strategy_persistence_app` plus `dashboard` when the UI profile is enabled

## Step 6: Verify Runtime Startup And Containers

Inspect startup logs:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "sudo tail -n 200 /var/log/option-trading-runtime-startup.log"
```

Check Compose services:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps"
```

Check runtime logs and data files:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml logs --tail 120 strategy_app"
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml logs --tail 120 snapshot_app"
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "tail -n 5 /opt/option_trading/.run/snapshot_app/events.jsonl"
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "tail -n 5 /opt/option_trading/.run/strategy_app/signals.jsonl"
```

Optional dashboard health:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "curl -fsS http://127.0.0.1:8008/api/health"
```

Look for:

- `strategy_app` starts with `engine=ml_pure`
- resolved run-id or artifact paths are present
- snapshot events continue to advance
- signals file exists and updates when snapshots arrive
- dashboard health succeeds if the dashboard profile is enabled

If `snapshot_app` is unhealthy and logs show repeated market-data or auth failures, verify Kite credentials were refreshed and published. The supported recovery path is to rerun `start_runtime_interactive.sh` with Kite browser auth enabled, then restart the VM again.

## Step 7: Rollback

If the new deploy is bad:

1. restore the previous runtime handoff or previous `.env.compose`
2. republish runtime config
3. restart the runtime VM

Commands:

```bash
./ops/gcp/publish_runtime_config.sh
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

Verify:

- runtime returns to the previous known-good run-id or config
- service logs stop showing the new failure mode

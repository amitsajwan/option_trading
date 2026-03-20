# Live Runtime Runbook

Use this runbook to build images, publish runtime config, start the live containers on GCP, validate them, and roll back if needed.

This workflow is self-contained. It includes the GCP setup it needs.

## What This Produces

- runtime images in Artifact Registry
- runtime config bundle in the runtime-config bucket
- live runtime VM running the Compose stack

## Step 1: Prepare Shared GCP Resources

If the runtime VM, buckets, or Artifact Registry do not exist yet:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

You need at least these values in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `RUNTIME_NAME`
- `REPOSITORY`
- `TAG`
- `MODEL_BUCKET_NAME`
- `RUNTIME_CONFIG_BUCKET_NAME`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`

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

## Step 2: Build And Push Runtime Images

```bash
export PROJECT_ID REGION REPOSITORY TAG
./ops/gcp/build_runtime_images.sh
```

Verify:

- the command exits successfully
- Cloud Build shows successful image builds for the selected services

Optional verification:

```bash
gcloud artifacts docker images list "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}"
```

Look for:

- the expected services under the chosen tag

## Step 3: Prepare Runtime Config

If training just produced a new release handoff:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_id>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

Then verify `.env.compose` contains the supported live settings:

```env
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=<published_run_id>
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
STRATEGY_ROLLOUT_STAGE=capped_live
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE=.run/ml_runtime_guard_live.json
```

Verify:

```bash
grep -E "STRATEGY_ENGINE|ML_PURE_RUN_ID|ML_PURE_MODEL_GROUP|STRATEGY_ROLLOUT_STAGE|STRATEGY_POSITION_SIZE_MULTIPLIER|STRATEGY_ML_RUNTIME_GUARD_FILE" .env.compose
test -f .run/ml_runtime_guard_live.json && echo guard_ok
```

Look for:

- all required env keys are present
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

## Step 5: Start Or Restart The Runtime VM

```bash
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

Verify:

```bash
gcloud compute instances describe "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- `RUNNING`

## Step 6: Verify Runtime Startup And Containers

Verify:

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

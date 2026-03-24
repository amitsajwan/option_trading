# Live Runtime Runbook

Use this runbook to build images, publish runtime config, start the live containers on GCP, validate them, and roll back if needed.

This workflow is self-contained. It includes the GCP setup it needs.

## Fast Path (Interactive)

If you want a guided prompt that asks for all required runtime values and runs publish + optional VM restart:

```bash
./ops/gcp/start_runtime_interactive.sh
```

It prompts for:

- project/region/zone/runtime VM name
- runtime-config bucket URL
- `GHCR_IMAGE_PREFIX`
- `APP_IMAGE_TAG`
- `ML_PURE_RUN_ID`
- `ML_PURE_MODEL_GROUP`

## What This Produces

- runtime images in GHCR (published by GitHub Actions)
- runtime config bundle in the runtime-config bucket
- live runtime VM running the Compose stack

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

## Step 2: Select GHCR Image Tag

Images are published by `.github/workflows/build-images.yml` to:

- `ghcr.io/<owner>/<service>:latest` on `main`
- `ghcr.io/<owner>/<service>:<short_sha>` on each push

Set these values in `ops/gcp/operator.env` and `.env.compose`:

```env
GHCR_IMAGE_PREFIX=ghcr.io/amitsajwan
APP_IMAGE_TAG=latest
```

If packages are private, also set in `.env.compose`:

```env
GHCR_USERNAME=<github-user>
GHCR_TOKEN=<github-read-packages-token>
```

Minimum image set for full runtime stack:

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

## Step 3: Prepare Runtime Config

If training just produced a new release handoff:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_id>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

`apply_ml_pure_release.sh` only writes the staged handoff keys:

- `STRATEGY_ENGINE`
- `ML_PURE_RUN_ID`
- `ML_PURE_MODEL_GROUP`

It does not make the repo live-ready by itself. Before you publish runtime config, make sure `.env.compose` also contains the live rollout and monitoring prerequisites that the runtime and dashboard expect.

Then verify `.env.compose` contains the supported live settings:

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
test -f ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/threshold_report.json && echo threshold_ok
test -f ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/training_report.json && echo training_report_ok
```

Look for:

- all required env keys are present
- GHCR image prefix and tag are present
- guard file exists locally
- threshold report exists locally if rolling Stage 1 precision monitoring is expected
- training summary exists locally if regime-drift monitoring is expected

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

- Restarting the VM is the supported rollout path because startup syncs runtime config and published model artifacts, validates runtime bundle inputs, and then pulls/starts the pinned-tag images.
- The runtime startup stack includes `redis`, `mongo`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_app`, and `strategy_persistence_app` (plus `dashboard` when UI profile is enabled).

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

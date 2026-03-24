# GCP Deployment Runbook

Use this runbook as the release-manager source of truth for GCP runtime operations.

There is one preferred operator path:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

This runbook is organized around that path. Direct scripts and raw `gcloud` commands are included only as supporting references.

Important scope rule:

- `runtime_lifecycle_interactive.sh` is the preferred operator path for `Infra`, `Live`, and `Historical`
- `Historical` now has its own interactive branch in that same menu and is not driven by `start_runtime_interactive.sh`
- successful training `PUBLISH` writes the current approved runtime release artifacts that `Live` auto-loads by default

## Operating Model

Use the same release-manager structure every time:

0. `Infra`
1. `Live`
2. `Historical`

The intent is simple:

- `Infra` creates and validates the shared GCP foundation
- `Live` deploys or updates the always-on runtime
- `Historical` replays old dates without disturbing the live lane

For a first-time setup, do `0. Infra` first, then `1. Live`. Run `2. Historical` only when you need replay.

Use this decision rule:

- run `0. Infra` once per environment, or again only when infra changes
- run `1. Live` for normal runtime deploys and restarts
- run `2. Historical` only for replay analysis; it does not require rerunning infra unless you need a separate replay VM

## First-Time Operator Checklist

Before starting:

- work from Ubuntu, WSL, or Cloud Shell
- run from the repo root
- ensure `gcloud`, `terraform`, `docker`, and `bash` are available
- ensure `ops/gcp/operator.env` exists
- ensure `.env.compose` contains the intended live runtime values before you deploy

If `ops/gcp/operator.env` does not exist yet:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

If `.env.compose` does not exist yet:

```bash
cp .env.compose.example .env.compose
```

## 0. Infra

Use this section when the environment is new or when you need to confirm the shared GCP foundation is still healthy.

### Preferred Path

Run:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose:

1. `Bootstrap infra`

That flow writes `ops/gcp/operator.env`, derives bucket URLs, and can run the bootstrap immediately.

### Required Operator Values

These fields must be correct in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `RUNTIME_NAME`
- `TAG`
- `GHCR_IMAGE_PREFIX`
- `MODEL_BUCKET_NAME`
- `RUNTIME_CONFIG_BUCKET_NAME`

Current conventions:

- runtime images come from GHCR
- `REPOSITORY` still exists only for Terraform and bootstrap compatibility
- `MODEL_BUCKET_URL` defaults to `gs://<MODEL_BUCKET_NAME>/published_models`
- `RUNTIME_CONFIG_BUCKET_URL` defaults to `gs://<RUNTIME_CONFIG_BUCKET_NAME>/runtime`

### First-Time Bootstrap Reference

If you need the non-menu path:

```bash
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Use `RUN_RUNTIME_CONFIG_SYNC=0` on a fresh checkout when `.env.compose` is still a template and should not be published yet.

### Infra Verification

Run:

```bash
cd infra/gcp
terraform output
gcloud compute instances describe "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --format="value(status)"
gcloud storage ls "gs://${MODEL_BUCKET_NAME}"
gcloud storage ls "gs://${RUNTIME_CONFIG_BUCKET_NAME}"
```

You should see:

- Terraform outputs succeed
- the runtime VM exists
- the model bucket exists
- the runtime-config bucket exists

## 1. Live

Use this section for the always-on production runtime.

This is the only supported lane for live market deployment. Historical replay is separate.

### Preferred Path

Run:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose:

2. `Start or restart runtime deploy`

This is the best path because it keeps the operator on the supported sequence:

1. fetch the current approved runtime release manifest and runtime env from the runtime-config bucket
2. apply the runtime handoff into `.env.compose`
3. show Kite credential state and fail closed until it is valid
4. run shared live preflight
5. publish runtime config
6. start or restart the runtime VM

### Live Deployment Inputs

The interactive deploy helper expects:

- project, region, zone, and runtime VM name
- runtime-config bucket URL
- `GHCR_IMAGE_PREFIX`
- release manifest path only when overriding the current approved release

The helper also supports:

- auto-download of:
  - `release/current_runtime_release.json`
  - `release/current_ml_pure_runtime.env`
- optional override to a different runtime release manifest
- Kite browser auth through `python -m ingestion_app.kite_auth --force`
- prompting for `KITE_API_KEY` and hidden `KITE_API_SECRET` when needed
- syncing `KITE_API_KEY` and `KITE_ACCESS_TOKEN` from `ingestion_app/credentials.json` into `.env.compose`
- automatic `INGESTION_COLLECTORS_ENABLED=1`
- shared preflight for release manifest, runtime bundle, GHCR image tag, and Kite state

### Live Runtime Contract

Before deploying, `.env.compose` must contain a coherent live runtime config.

Required live settings:

```env
GHCR_IMAGE_PREFIX=ghcr.io/amitsajwan
APP_IMAGE_TAG=latest
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=<published_run_id>
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
STRATEGY_ROLLOUT_STAGE=capped_live
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE=.run/ml_runtime_guard_live.json
```

Supported monitoring inputs:

```env
ML_PURE_THRESHOLD_REPORT=ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/threshold_report.json
ML_PURE_TRAINING_SUMMARY_PATH=ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/training_report.json
```

Notes:

- `publish_runtime_config.sh` validates the capped-live fields and the guard file before publishing an `ml_pure` runtime config
- `docker-compose.gcp.yml` expects `GHCR_IMAGE_PREFIX` and `APP_IMAGE_TAG`
- use repo-relative artifact paths so the runtime VM can sync them through the runtime-config bundle
- the current approved release manifest provides:
  - `APP_IMAGE_TAG`
  - `ML_PURE_RUN_ID`
  - `ML_PURE_MODEL_GROUP`
  - `ML_PURE_THRESHOLD_REPORT`
  - `ML_PURE_TRAINING_SUMMARY_PATH`
  - `STRATEGY_ML_RUNTIME_GUARD_FILE`

### Current Approved Release Artifacts

Successful training `PUBLISH` now writes:

- run-local manifest: `release/runtime_release_manifest.json`
- repo-local current manifest cache: `.run/gcp_release/current_runtime_release.json`
- repo-local current pointer: `.run/gcp_release/current_runtime_release_pointer.json`
- repo-local current runtime env copy: `.run/gcp_release/current_ml_pure_runtime.env`

It also uploads the current artifacts to the runtime-config bucket under:

- `release/current_runtime_release.json`
- `release/current_runtime_release_pointer.json`
- `release/current_ml_pure_runtime.env`

`Live` auto-picks these files by default, so the operator does not need to type run-id or model-group manually for the normal deploy path.

### Live Verification

After the interactive deploy completes, verify the runtime from the VM.

Startup log:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "sudo tail -n 200 /var/log/option-trading-runtime-startup.log"
```

Compose status:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps"
```

Key service logs:

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

You should see:

- the VM is `RUNNING`
- `strategy_app` starts with `engine=ml_pure`
- snapshots continue to advance
- strategy signals update when snapshots arrive
- dashboard health succeeds if the UI profile is enabled

### Live Rollback

If a live deploy is bad:

1. restore the previous `.env.compose` or release handoff
2. republish runtime config
3. restart the runtime VM

Reference commands:

```bash
./ops/gcp/publish_runtime_config.sh
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

## 2. Historical

Use this section for old-date simulation through the supported snapshot replay stack.

Historical replay is a separate operator lane. It is not a live cutover path and it must not be mixed into the live stack by accident.

Do not use `start_runtime_interactive.sh` as the main entrypoint for historical replay. That flow is live-opinionated and can push live settings such as `STRATEGY_ROLLOUT_STAGE=capped_live`, `INGESTION_COLLECTORS_ENABLED=1`, and Kite auth prompts that are not part of replay execution.

Use the same lifecycle menu instead:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose:

3. `Historical replay`

### Historical Rules

Keep these boundaries hard:

- use the same pinned GHCR image tags as live
- use only the `historical` and `historical_replay` Compose profiles
- replay only from stored historical snapshots
- never publish replay data to live Redis topics
- never write replay data into live Mongo collections
- do not use the archived dashboard backtest flow as the GCP historical path
- once historical snapshots already exist, replay does not require Kite auth or `ingestion_app/credentials.json`

### Preferred Operating Shape

Default:

- use the runtime VM, but start only the historical profiles and keep the live lane untouched

Optional:

- use a separate replay VM with the same repo checkout and compose files when you want stronger isolation

Do not use the snapshot-build VM as the default replay target unless you are intentionally combining build and replay for one-off analysis.

### Upstream Artifact Build

Historical parquet is a separate runtime input artifact. It is not part of the normal runtime-config bundle.

Build and publish parquet first by following [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) and using:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Do not use `publish_runtime_config.sh` to ship parquet datasets. Keep that bundle limited to `.env.compose`, optional credentials, runtime guards, and small referenced runtime artifacts.

Do not upload historical parquet into the runtime-config bucket.

### Canonical Historical Sequence

Use this exact sequence:

1. build and publish historical parquet to GCS
2. sync the required parquet subset onto the target VM under `/opt/option_trading/.data/ml_pipeline/parquet_data`
3. start the historical consumers
4. run the one-shot replay job for the target date or date range
5. inspect historical outputs in Redis, Mongo, and the dashboard

The interactive historical branch performs this sequence by prompting for:

- replay VM name
- GHCR image prefix and tag
- snapshot parquet bucket URL
- replay start date and end date
- replay speed
- local historical preflight before any remote work
- remote historical preflight after parquet sync
- whether to publish the current runtime config bundle first
- whether to sync parquet now
- whether to run the replay job now

If you only remember one thing for replay, remember this:

1. parquet already published
2. parquet synced onto the replay VM
3. only historical profiles started
4. one-shot replay run
5. historical outputs verified

### Historical Preflight

Do not start replay until all of these are true:

- target parquet is present under `/opt/option_trading/.data/ml_pipeline/parquet_data`
- target date is present in the synced dataset
- replay topic resolves to `market:snapshot:v1:historical`
- historical Mongo collection env vars are in effect
- you will use `--profile historical` and `--profile historical_replay`
- no one is planning to restart the live VM just to run replay

### Stop Conditions

Stop immediately and do not continue if any of these are true:

- replay topic resolves to the live snapshot topic
- historical collections are not configured
- parquet sync is incomplete or the target date is absent
- replay is being attempted on the live runtime VM during active market hours without explicit approval

### Historical Sync And Replay Reference

Sync parquet onto the target VM:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "
  sudo mkdir -p /opt/option_trading/.data/ml_pipeline/parquet_data &&
  sudo gcloud storage rsync '${SNAPSHOT_PARQUET_BUCKET_URL%/}' '/opt/option_trading/.data/ml_pipeline/parquet_data' --recursive
"
```

Start historical consumers:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "
  cd /opt/option_trading &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical
"
```

Run one-shot replay:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "
  cd /opt/option_trading &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical_replay run --rm historical_replay --start-date 2026-03-06 --end-date 2026-03-06 --speed 0
"
```

Verify replay services:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "
  cd /opt/option_trading &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml logs --tail 120 strategy_app_historical &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml logs --tail 120 strategy_persistence_app_historical
"
```

Optional replay dashboard checks:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "curl -fsS http://127.0.0.1:8008/api/health/replay"
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "curl -fsS http://127.0.0.1:8008/api/historical/replay/status"
```

You should see:

- replay emits only to `market:snapshot:v1:historical`
- `strategy_app_historical` consumes historical snapshots only
- historical strategy outputs stay on `*:historical` topics
- historical persistence writes only to historical collections
- live services remain unaffected when historical profiles are not started

Dashboard note:

- dashboard market-data and replay pages are usable for historical analysis
- some views remain live-oriented
- replay mode is not broker-faithful live emulation

## Direct Script Reference

Use these only when you explicitly need a lower-level entrypoint.

Interactive scripts:

- `bash ./ops/gcp/runtime_lifecycle_interactive.sh`
- `bash ./ops/gcp/bootstrap_runtime_interactive.sh`
- `bash ./ops/gcp/start_runtime_interactive.sh`
- `bash ./ops/gcp/start_training_interactive.sh`
- `bash ./ops/gcp/run_snapshot_parquet_pipeline.sh`

Support scripts:

- `./ops/gcp/from_scratch_bootstrap.sh`
- `./ops/gcp/publish_runtime_config.sh`
- `./ops/gcp/apply_ml_pure_release.sh`
- `./ops/gcp/stop_runtime.sh`
- `./ops/gcp/destroy_infra_preserve_data.sh`

## Clean Daily Pattern

For normal release management, keep the workflow stable:

1. run `0. Infra` only when the environment is new or changed
2. run `1. Live` for the always-on deployment
3. run `2. Historical` only when you need replay
4. keep live and historical operationally separate even when they share the same image tag and repo checkout

If you follow that pattern, a first-time operator can get the environment working without inventing alternate deployment paths.

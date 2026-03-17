# GCP Runtime Deploy Runbook

Use this when base infra already exists and you want to deploy or cut over the live runtime.

This runbook is intentionally narrow:

- build and push runtime images
- publish runtime config
- restart or recreate the runtime VM
- validate health
- roll back if needed

For Day 0 infra creation, use [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md).
For model publishing, use [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md).
For full stack Docker support checks, use [SUPPORT_BRINGUP_GUIDE.md](SUPPORT_BRINGUP_GUIDE.md).

## Resources In This Phase

Resources used in this phase:

- runtime VM
- Artifact Registry
- runtime-config bucket
- model bucket

Resources not required in this phase:

- training VM after model publish is complete
- snapshot-build VM

## Preconditions

- base infra exists
- `ops/gcp/operator.env` is current
- Artifact Registry repo exists
- runtime-config bucket exists
- runtime VM exists unless you intentionally tore it down

## Inputs To Verify

Check these values in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `RUNTIME_NAME`
- `REPOSITORY`
- `TAG`
- `RUNTIME_CONFIG_BUCKET_URL`

## Step 1: Build And Push Runtime Images

From repo root:

```bash
export PROJECT_ID REGION REPOSITORY TAG
./ops/gcp/build_runtime_images.sh
```

If you only want a subset of services:

```bash
export PROJECT_ID REGION REPOSITORY TAG
./ops/gcp/build_runtime_images.sh ingestion_app snapshot_app persistence_app strategy_app market_data_dashboard
```

Expected result:

- runtime images are present under Artifact Registry
- the chosen tag is the one the runtime VM should pull on boot

## Step 2: Publish Runtime Config

From repo root:

```bash
export RUNTIME_CONFIG_BUCKET_URL
./ops/gcp/publish_runtime_config.sh
```

This uploads:

- `.env.compose`
- optional `ingestion_app/credentials.json`

If training just produced a new ML runtime handoff, this step should happen after the handoff has been applied.

## Step 3: Restart Or Recreate The Runtime

If the runtime VM already exists, restart it so the startup script re-syncs config and pulls the new images:

```bash
gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
```

If you previously ran preserve-data teardown, recreate compute first:

```bash
RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Then restart if needed.

## Step 4: Validate Runtime Health

Basic VM status:

```bash
gcloud compute instances describe "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --format="value(status)"
```

If you need to inspect startup logs:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "sudo tail -n 200 /var/log/option-trading-runtime-startup.log"
```

For compose-level service checks on the VM:

```bash
gcloud compute ssh "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --command "cd /opt/option_trading && sudo docker compose -f docker-compose.yml -f docker-compose.gcp.yml ps"
```

For app-level functional checks after runtime is up, use [SUPPORT_BRINGUP_GUIDE.md](SUPPORT_BRINGUP_GUIDE.md).

## Rollback

If the new deploy is bad:

1. restore the previous runtime config or runtime handoff
2. republish runtime config
3. restart the runtime VM

If the issue is image-specific, republish the previously good image tag and restart again.

## Related Files

- [ops/gcp/build_runtime_images.sh](../ops/gcp/build_runtime_images.sh)
- [ops/gcp/publish_runtime_config.sh](../ops/gcp/publish_runtime_config.sh)
- [ops/gcp/README.md](../ops/gcp/README.md)

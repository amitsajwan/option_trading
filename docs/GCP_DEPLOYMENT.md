# GCP Deployment Runbook

This repo now supports a repeatable GCP deployment model with two machine roles:

- `runtime VM`: small, always-on, runs the live Docker Compose stack
- `training VM`: large, disposable, used only for research, tuning, and publishing

The design goal is simple:

- code images come from Artifact Registry
- published models come from Cloud Storage
- frozen ML inputs come from Cloud Storage
- infrastructure comes from Terraform
- VMs are disposable
- GitHub Actions orchestrates the repeatable deploy path

If you want the exact tear-down/rebuild sequence from an oversized legacy VM, use [GCP_FRESH_START.md](GCP_FRESH_START.md).
If you want the full operator step-by-step from zero, use [FROM_SCRATCH_OPERATOR_GUIDE.md](FROM_SCRATCH_OPERATOR_GUIDE.md).
If you want the runnable wrapper scripts, use [ops/gcp/README.md](../ops/gcp/README.md).

## 0. Recommended Automation Boundary

Use GitHub Actions for orchestration and GCP for execution.

Recommended split:

- GitHub Actions:
  - CI
  - image builds
  - Terraform plan/apply
  - manual training-release dispatch
  - controlled runtime deploys
- GCP:
  - runtime VM
  - training VM
  - Artifact Registry
  - Cloud Storage

This is the preferred path over a permanently manual operator-machine workflow.

## 1. Recommended Architecture

### Runtime VM

Use one smaller VM for the supported live stack:

- `redis`
- `mongo`
- `ingestion_app`
- `snapshot_app`
- `persistence_app`
- `strategy_app`
- optional `dashboard`

The runtime VM should not be a build machine and should not be the source of truth for models.

### Training VM

Use a separate high-memory VM only when you need to:

- run `ml_pipeline_2` research
- run matrix jobs
- run threshold sweeps
- publish a selected model

When training is done, sync published artifacts to GCS and delete the VM.

## 2. Source Of Truth

### Container images

Store runtime images in Artifact Registry.

This repo now includes [docker-compose.gcp.yml](../docker-compose.gcp.yml), which maps services to Artifact Registry image names instead of relying on per-VM builds.

### Published models

Store published runtime model artifacts in Cloud Storage under a dedicated prefix, for example:

```text
gs://<model-bucket>/published_models/
```

Those artifacts originate from the local publisher output under:

```text
ml_pipeline_2/artifacts/published_models/
```

### Frozen ML inputs

Keep training inputs in Cloud Storage and sync them locally on the training VM to:

```text
.data/ml_pipeline/
```

## 3. First-Time Setup

### Provision infra

Use the Terraform scaffold in [infra/gcp/README.md](../infra/gcp/README.md):

- Artifact Registry repository
- model bucket
- runtime config bucket
- runtime VM
- training VM instance template
- service accounts and IAM

### Build and push runtime images

Use [ops/gcp/build_runtime_images.sh](../ops/gcp/build_runtime_images.sh):

```bash
export PROJECT_ID=<gcp-project>
export REGION=asia-south1
export REPOSITORY=option-trading-runtime
export TAG=20260317-1

./ops/gcp/build_runtime_images.sh
```

This builds the distinct runtime images with Cloud Build and pushes them to Artifact Registry.

### Publish runtime bootstrap bundle

Use [ops/gcp/publish_runtime_config.sh](../ops/gcp/publish_runtime_config.sh):

```bash
export RUNTIME_CONFIG_BUCKET_URL=gs://<runtime-config-bucket>/runtime
./ops/gcp/publish_runtime_config.sh
```

This uploads the runtime `.env.compose` and optional `ingestion_app/credentials.json` bundle expected by the VM startup script.

## 4. Publish Model Artifacts

After a training run is selected, prefer the guarded end-to-end release command:

```bash
python -m ml_pipeline_2.run_recovery_release \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.best_1m_e2e.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

That single flow will:

1. run training or reuse an existing run
2. run threshold sweep
3. block non-promotable candidates by default
4. publish locally
5. sync the selected model group to GCS
6. write the `ML_PURE_*` runtime handoff env file for live/eval deployment

To apply that handoff into the runtime compose env on the operator machine or runtime repo checkout, use [ops/gcp/apply_ml_pure_release.sh](../ops/gcp/apply_ml_pure_release.sh):

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

If you also want to refresh the bootstrap bundle in GCS in the same step:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>/release/ml_pure_runtime.env
export AUTO_PUBLISH_RUNTIME_CONFIG=1
export RUNTIME_CONFIG_BUCKET_URL=gs://<runtime-config-bucket>/runtime
./ops/gcp/apply_ml_pure_release.sh
```

Use [ops/gcp/publish_published_models.sh](../ops/gcp/publish_published_models.sh):

```bash
export MODEL_BUCKET_URL=gs://<model-bucket>/published_models
./ops/gcp/publish_published_models.sh
```

That keeps runtime model resolution compatible with the existing filesystem layout expected by `strategy_app` and `market_data_dashboard`.

## 5. Runtime Bootstrap

The Terraform runtime VM startup script does this automatically:

1. installs Docker and Google Cloud CLI
2. clones the repo at the configured ref
3. syncs runtime config bundle from GCS
4. syncs published models from GCS into `ml_pipeline_2/artifacts/published_models`
5. optionally syncs frozen data into `.data/ml_pipeline`
6. authenticates Docker to Artifact Registry
7. runs:

```bash
docker compose -f docker-compose.yml -f docker-compose.gcp.yml pull
docker compose -f docker-compose.yml -f docker-compose.gcp.yml up -d ...
```

This avoids `nohup python ...` drift and keeps the VM reproducible.

## 6. Runtime Config Bundle

For the first pass, keep a restricted GCS prefix for runtime bootstrap files, for example:

```text
gs://<runtime-config-bucket>/runtime/
```

Recommended contents:

- `.env.compose`
- `ingestion_app/credentials.json`
- optional extra operator files

The bootstrap script copies those into the checked-out repo before starting Compose.

If you want stricter secret handling later, move `credentials.json` and similar secrets to Secret Manager. The rest of this deployment shape still holds.

## 7. Updating Runtime

For a normal runtime rollout:

1. push code to git
2. build/push new images to Artifact Registry
3. publish/sync new models to GCS if needed
4. update `APP_IMAGE_TAG` or `repo_ref`
5. restart the runtime VM or rerun the bootstrap steps

Because the runtime VM is disposable, rebuilding the VM is an acceptable deployment method.

## 8. Why This Fixes The 8008 Problem

The recent `http://34.100.249.76:8008/trading/models` issue came from ad hoc process management and inconsistent network paths.

This deployment plan removes that class of problem by:

- using one Compose-managed runtime path
- avoiding host-side `nohup python ...` services
- making the VM boot into one known stack
- separating code deployment from model artifact deployment

## 9. Expert Defaults

For this repo, the pragmatic expert default is:

- Terraform for infra
- GitHub Actions for orchestration
- Cloud Build + Artifact Registry for app images
- Cloud Storage for published models and frozen data
- Docker Compose on the runtime VM
- ephemeral training VMs

That is enough structure to stop doing machine-by-machine manual setup without requiring a full Kubernetes migration.

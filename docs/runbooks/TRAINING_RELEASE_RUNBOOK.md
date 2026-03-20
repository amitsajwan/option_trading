# Training Release Runbook

Use this runbook to train, publish, and generate the staged `ml_pure` runtime handoff.

This workflow is self-contained. It includes the GCP setup it needs.

## What This Produces

- one disposable training VM
- a completed staged research run
- published staged model artifacts in the model bucket
- updated runtime config bundle in the runtime-config bucket
- `release/ml_pure_runtime.env`

## Step 1: Prepare Shared GCP Resources

If the shared training/runtime resources do not exist yet, create them first:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

You need at least these values in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `REPO_CLONE_URL`
- `REPO_REF`
- `MODEL_BUCKET_NAME`
- `RUNTIME_CONFIG_BUCKET_NAME`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`
- `DATA_SYNC_SOURCE`
- `TRAINING_VM_NAME`
- `MODEL_GROUP`
- `PROFILE_ID`
- `STAGED_CONFIG`

Verify:

```bash
cd infra/gcp
terraform output
gcloud storage ls "gs://${MODEL_BUCKET_NAME}"
gcloud storage ls "gs://${RUNTIME_CONFIG_BUCKET_NAME}"
```

Look for:

- Terraform outputs succeed
- both buckets exist
- the training instance template exists in Terraform output

## Step 2: Create The Disposable Training VM

```bash
./ops/gcp/create_training_vm.sh
```

Verify:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- `RUNNING`

## Step 3: Verify VM Startup Sync

SSH to the VM:

```bash
gcloud compute ssh "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

On the VM:

```bash
cd /opt/option_trading
git rev-parse --short HEAD
find .data/ml_pipeline/parquet_data -maxdepth 2 -type d | sort
```

Verify:

Look for:

- repo checkout exists under `/opt/option_trading`
- parquet datasets are present locally
- at minimum:
  - `snapshots_ml_flat`
  - `stage1_entry_view`
  - `stage2_direction_view`
  - `stage3_recipe_view`

If those datasets are missing, stop here and complete the snapshot workflow first.

## Step 4: Run The Staged Release Pipeline

On the training VM:

```bash
cd /opt/option_trading
./ops/gcp/run_staged_release_pipeline.sh
```

Verify:

- command exits successfully
- output includes `Staged release pipeline complete`
- output prints the `runtime handoff` path

Also verify the latest local release handoff:

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/ml_pure_runtime.env" | sort | tail -n 1
```

Look for:

- a concrete `release/ml_pure_runtime.env` path

## Step 5: Verify Publish Results

Verify:

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/research -name summary.json | sort | tail -n 1
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/release_summary.json" | sort | tail -n 1
```

Look for:

- a `summary.json`
- a `release/release_summary.json`

Check the buckets:

```bash
gcloud storage ls "${MODEL_BUCKET_URL}"
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

Look for:

- the published model group under the model bucket
- runtime config bundle files under the runtime-config bucket

What to inspect in the run summary:

- `publish_assessment.decision` should be `PUBLISH`
- `publish_assessment.publishable` should be `true`
- `blocking_reasons` should be empty

If the staged release returns `HOLD`, stop and investigate the holdout gates before live deployment.

## Step 6: Delete Temporary Training Infra

Delete the disposable training VM after publish is complete:

```bash
./ops/gcp/delete_training_vm.sh
```

Verify:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- instance not found

Keep these shared resources if live runtime still needs them:

- model bucket
- runtime-config bucket
- Artifact Registry
- runtime VM

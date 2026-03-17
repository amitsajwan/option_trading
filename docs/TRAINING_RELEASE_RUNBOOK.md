# Training Release Runbook

Use this when base GCP infrastructure already exists and you want to create a disposable training VM, run the guarded ML release flow, and publish the runtime handoff.

## Audience

- ML operator
- release manager running a scheduled model release

## Preconditions

- [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md) is already complete
- `ops/gcp/operator.env` is filled and current
- the repo branch in `REPO_REF` contains the intended ML release code
- training data and published model bucket paths are correct

## Inputs To Verify

Check these values in `ops/gcp/operator.env` before starting:

- `PROJECT_ID`
- `ZONE`
- `TRAINING_VM_NAME`
- `MODEL_GROUP`
- `PROFILE_ID`
- `RECOVERY_CONFIG`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`

## Step 1: Create The Disposable Training VM

From repo root:

```bash
./ops/gcp/create_training_vm.sh
```

Expected result:

- a VM named `TRAINING_VM_NAME` exists in the configured zone
- it was created from the Terraform training instance template

Validation:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

## Step 2: Connect To The Training VM

Example:

```bash
gcloud compute ssh "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

On the VM, go to the repo checkout:

```bash
cd /opt/option_trading
git fetch --all --tags
git checkout "${REPO_REF}"
git pull --ff-only
```

## Step 3: Run The Guarded Release Flow

On the training VM:

```bash
./ops/gcp/run_recovery_release_pipeline.sh
```

What this does:

1. creates or reuses `.venv`
2. installs `ml_pipeline_2`
3. runs `ml_pipeline_2.run_recovery_release`
4. applies the generated `ML_PURE_*` handoff to `.env.compose`
5. republishes runtime config to the runtime-config bucket

## Expected Result

Successful release should produce:

- a release JSON payload
- a runtime handoff env file
- published model artifacts under `MODEL_BUCKET_URL`
- refreshed runtime config under `RUNTIME_CONFIG_BUCKET_URL`

## Validation

Inspect published models:

```bash
gcloud storage ls "${MODEL_BUCKET_URL}"
```

Inspect runtime config bundle:

```bash
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

If you need the exact runtime handoff location, the script prints it at the end.

## After Training

If the new release is approved, continue with [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) to restart or cut over runtime.

If you are done with the VM, remove it:

```bash
./ops/gcp/delete_training_vm.sh
```

## Failure Signals

Stop and investigate if:

- release output remains `HOLD`
- the published model bucket does not get new artifacts
- runtime config is not republished
- the training VM has stale code or wrong branch checked out

## Related Files

- [ops/gcp/run_recovery_release_pipeline.sh](../ops/gcp/run_recovery_release_pipeline.sh)
- [ops/gcp/create_training_vm.sh](../ops/gcp/create_training_vm.sh)
- [ops/gcp/delete_training_vm.sh](../ops/gcp/delete_training_vm.sh)
- [ops/gcp/README.md](../ops/gcp/README.md)

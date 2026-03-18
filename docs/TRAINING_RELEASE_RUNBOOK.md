# Staged Training Release Runbook

Use this when base GCP infrastructure already exists and you want to create a disposable training VM, run the staged 1/2/3 training and publish flow, and generate the live runtime handoff.

## Audience

- ML operator
- release manager running a scheduled model release

## Resources In This Phase

Resources used in this phase:

- training VM template
- one disposable training VM
- model bucket
- runtime-config bucket
- existing snapshot parquet and stage-view parquet

Resources not required for training itself:

- a running runtime VM
- runtime image build
- a snapshot-build VM

If the full bootstrap lane already created a runtime VM, it can stay stopped until deploy time.

## Preconditions

- [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md) is already complete
- final snapshot parquet already exists under the configured parquet root
- `ops/gcp/operator.env` is filled and current
- the repo branch in `REPO_REF` contains the intended staged ML code

## Inputs To Verify

Check these values in `ops/gcp/operator.env` before starting:

- `PROJECT_ID`
- `ZONE`
- `TRAINING_VM_NAME`
- `MODEL_GROUP`
- `PROFILE_ID`
- `STAGED_CONFIG`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`

`STAGED_CONFIG` should normally point at:

- `ml_pipeline_2/configs/research/staged_dual_recipe.default.json`

## Recommended Training VM Size

The training VM is created from the Terraform instance template controlled by `TRAINING_MACHINE_TYPE`.

Practical starting point:

- `n2-standard-8` for a normal staged release
- `n2-standard-16` if training is clearly CPU-bound on the full dataset

To change the size, update `TRAINING_MACHINE_TYPE`, rerun the Terraform/bootstrap lane, and then create the disposable training VM.

## Step 1: Create The Disposable Training VM

From repo root:

```bash
./ops/gcp/create_training_vm.sh
```

Validation:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

## Step 2: Connect To The Training VM

```bash
gcloud compute ssh "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

On the VM:

```bash
cd /opt/option_trading
git fetch --all --tags
git checkout "${REPO_REF}"
git pull --ff-only
```

## Step 3: Run The Staged Release Flow

On the training VM:

```bash
./ops/gcp/run_staged_release_pipeline.sh
```

What this does:

1. creates or reuses `.venv`
2. installs `ml_pipeline_2`
3. runs `ml_pipeline_2.run_staged_release`
4. applies the generated `ML_PURE_*` handoff to `.env.compose`
5. republishes runtime config to the runtime-config bucket

## What The Staged Flow Trains

The staged release flow trains and evaluates:

1. Stage 1 entry model
2. Stage 2 direction model
3. Stage 3 recipe-selection models

It selects policy only on `research_valid`, scores `final_holdout` once, and publishes only if all staged and combined hard gates pass.

## Expected Result

Successful release should produce:

- a release JSON payload
- `release/ml_pure_runtime.env`
- published model artifacts under `MODEL_BUCKET_URL`
- refreshed runtime config under `RUNTIME_CONFIG_BUCKET_URL`

Published bundle contents:

- Stage 1 model package
- Stage 2 model package
- Stage 3 recipe packages
- recipe catalog
- staged runtime policy
- holdout and publish assessment reports

## Validation

Inspect published models:

```bash
gcloud storage ls "${MODEL_BUCKET_URL}"
```

Inspect runtime config bundle:

```bash
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

Inspect the runtime handoff file locally on the VM:

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/ml_pure_runtime.env" | sort | tail -n 1
```

The runtime handoff should contain:

- `STRATEGY_ENGINE=ml_pure`
- `ML_PURE_RUN_ID=<published_run_id>`
- `ML_PURE_MODEL_GROUP=<model_group>`

## After Training

If the staged release is approved, continue with [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) to restart or cut over runtime.

If you are done with the VM, remove it:

```bash
./ops/gcp/delete_training_vm.sh
```

## Failure Signals

Stop and investigate if:

- release output is `HOLD`
- the model bucket does not get new staged artifacts
- runtime config is not republished
- the training VM has stale code or the wrong branch checked out

## Related Files

- [ops/gcp/run_staged_release_pipeline.sh](../ops/gcp/run_staged_release_pipeline.sh)
- [ops/gcp/create_training_vm.sh](../ops/gcp/create_training_vm.sh)
- [ops/gcp/delete_training_vm.sh](../ops/gcp/delete_training_vm.sh)
- [ops/gcp/apply_ml_pure_release.sh](../ops/gcp/apply_ml_pure_release.sh)
- [ml_pipeline_2/configs/research/staged_dual_recipe.default.json](../ml_pipeline_2/configs/research/staged_dual_recipe.default.json)
- [ops/gcp/README.md](../ops/gcp/README.md)

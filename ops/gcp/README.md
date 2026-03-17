# GCP Operator Scripts

These scripts are the runnable operator layer over the Terraform, Docker, and ML release pieces.

Run them from:

- Ubuntu
- WSL
- Cloud Shell

They assume a Bash environment.

These scripts are also the intended execution layer for future GitHub Actions workflows. The workflow YAML should call these scripts instead of re-implementing the same logic.

## Files To Copy First

1. Copy [operator.env.example](/c:/code/option_trading/ops/gcp/operator.env.example) to `ops/gcp/operator.env`
2. Fill in your actual project, bucket, repo, and release values

## Main Scripts

### `from_scratch_bootstrap.sh`

Use this from a fresh operator machine or repo checkout to:

1. write `infra/gcp/terraform.tfvars`
2. run Terraform
3. build and push runtime images
4. publish the runtime config bundle

Example:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
./ops/gcp/from_scratch_bootstrap.sh
```

### `create_training_vm.sh`

Use this after Terraform has been applied to create a disposable training VM from the Terraform output instance template.

Example:

```bash
./ops/gcp/create_training_vm.sh
```

### `run_recovery_release_pipeline.sh`

Use this on the training VM or a repo checkout that has the training data and runtime config available.

It will:

1. ensure the virtualenv exists
2. install `ml_pipeline_2`
3. run the guarded recovery release flow
4. apply the generated `ML_PURE_*` handoff into `.env.compose`
5. republish the runtime config bundle

Example:

```bash
./ops/gcp/run_recovery_release_pipeline.sh
```

### `apply_ml_pure_release.sh`

Use this when you already have a `release/ml_pure_runtime.env` file and only want to update `.env.compose`.

### `build_runtime_images.sh`

Builds and pushes runtime images to Artifact Registry.

### `publish_runtime_config.sh`

Uploads `.env.compose` and optional ingestion credentials to the runtime config GCS prefix.

### `publish_published_models.sh`

Syncs the local `published_models` tree to the model bucket.

## Recommended Use

For a clean rebuild, the normal order is:

1. `from_scratch_bootstrap.sh`
2. `create_training_vm.sh`
3. `run_recovery_release_pipeline.sh`

For the full human-facing procedure, use [FROM_SCRATCH_OPERATOR_GUIDE.md](/c:/code/option_trading/docs/FROM_SCRATCH_OPERATOR_GUIDE.md).

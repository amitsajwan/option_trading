# GCP Operator Scripts

These scripts are the runnable operator layer over the Terraform, Docker, and ML release pieces.

Run them from:

- Ubuntu
- WSL
- Cloud Shell

They assume a Bash environment.

These scripts are also the intended execution layer for future GitHub Actions workflows. The workflow YAML should call these scripts instead of re-implementing the same logic.

## Files To Copy First

1. Copy [operator.env.example](operator.env.example) to `ops/gcp/operator.env`
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

### `delete_training_vm.sh`

Use this to delete a disposable training VM when you are done with training.

By default it uses `TRAINING_VM_NAME` from `operator.env`, but you can also pass a VM name as the first argument.

Example:

```bash
./ops/gcp/delete_training_vm.sh
```

or:

```bash
./ops/gcp/delete_training_vm.sh option-trading-training-02
```

### `run_staged_release_pipeline.sh`

Use this on the training VM or a repo checkout that has the training data and runtime config available.

It will:

1. ensure the virtualenv exists
2. install `ml_pipeline_2`
3. run the staged 1/2/3 release flow
4. apply the generated `ML_PURE_*` handoff into `.env.compose`
5. republish the runtime config bundle

Example:

```bash
./ops/gcp/run_staged_release_pipeline.sh
```

### `apply_ml_pure_release.sh`

Use this when you already have a `release/ml_pure_runtime.env` file and only want to update `.env.compose`.

### `build_runtime_images.sh`

Builds and pushes runtime images to Artifact Registry.

### `publish_runtime_config.sh`

Uploads `.env.compose` and optional ingestion credentials to the runtime config GCS prefix.

### `publish_published_models.sh`

Syncs the local `published_models` tree to the model bucket.

### `publish_raw_market_data.sh`

Syncs a local raw `banknifty_data` archive to a GCS prefix such as `RAW_ARCHIVE_BUCKET_URL`.

Use this once when you want the full raw archive accessible from disposable GCP build machines.

### `run_snapshot_parquet_pipeline.sh`

Builds final historical parquet from the raw archive or existing normalized cache.

It will:

1. optionally sync raw archive from GCS
2. create or reuse `.venv`
3. run the final historical snapshot builder
4. write build and validation reports
5. optionally upload final parquet to GCS

Important runtime knobs:

- `SNAPSHOT_JOBS`
- `SNAPSHOT_SLICE_MONTHS`
- `SNAPSHOT_SLICE_WARMUP_DAYS`

The current fast path uses chunked snapshot partitions with warmup continuity, not calendar-year-only workers.

### `publish_snapshot_parquet.sh`

Syncs local final parquet outputs to `SNAPSHOT_PARQUET_BUCKET_URL`.

By default it uploads:

- canonical `snapshots`
- derived `snapshots_ml_flat`
- snapshot build reports

Optionally it can also upload normalized parquet cache.

### `stop_runtime.sh`

Stops the always-on runtime VM without deleting any persistent resources.

Use this for a cheap idle state when you want to pause compute cost but keep:

- Artifact Registry images
- published models in GCS
- runtime config in GCS
- Terraform-managed infra definitions

Example:

```bash
./ops/gcp/stop_runtime.sh
```

### `destroy_infra_preserve_data.sh`

Destroys the Terraform-managed compute/network/IAM resources while preserving:

- Artifact Registry repository
- published model bucket
- runtime config bucket

This is the script to use when you want to tear down most cost-bearing infrastructure but keep deployable state.

By default it will also delete the disposable training VM named in `TRAINING_VM_NAME` if it exists.

Examples:

```bash
./ops/gcp/destroy_infra_preserve_data.sh
```

```bash
AUTO_APPROVE=1 ./ops/gcp/destroy_infra_preserve_data.sh
```

If you truly want a full wipe including buckets and Artifact Registry, use plain `terraform destroy` from `infra/gcp` instead of this helper.

## Recommended Use

For a staged model release, the normal order is:

1. `from_scratch_bootstrap.sh`
2. `create_training_vm.sh`
3. `run_staged_release_pipeline.sh`

For a final historical parquet rebuild on a high-power machine, the normal order is:

1. `publish_raw_market_data.sh` once from a machine that has the archive locally
2. create or use a large disposable GCP VM
3. `run_snapshot_parquet_pipeline.sh`
4. delete the build VM after parquet is uploaded

For a cheap idle state after you are done:

1. `delete_training_vm.sh`
2. `stop_runtime.sh`

For a deeper teardown that still preserves images and published models:

1. `delete_training_vm.sh`
2. `destroy_infra_preserve_data.sh`

To recreate later after the preserve-data teardown:

1. `from_scratch_bootstrap.sh` with `RUN_IMAGE_BUILD=0`
2. skip image rebuild unless code changed
3. skip runtime config sync unless `.env.compose` changed

Example:

```bash
export PATH="$HOME/bin:$PATH"
RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

For the operator index, use [FROM_SCRATCH_OPERATOR_GUIDE.md](../../docs/FROM_SCRATCH_OPERATOR_GUIDE.md).
For Day 0 bootstrap, use [GCP_BOOTSTRAP_RUNBOOK.md](../../docs/GCP_BOOTSTRAP_RUNBOOK.md).
For the dedicated historical parquet procedure, use [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](../../docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md).
For model release, use [TRAINING_RELEASE_RUNBOOK.md](../../docs/TRAINING_RELEASE_RUNBOOK.md).
For runtime deploy and cutover, use [GCP_DEPLOYMENT.md](../../docs/GCP_DEPLOYMENT.md).
For cleanup and rollback, use [CLEANUP_ROLLBACK_RUNBOOK.md](../../docs/CLEANUP_ROLLBACK_RUNBOOK.md).

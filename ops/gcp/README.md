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
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Why `RUN_RUNTIME_CONFIG_SYNC=0` on a fresh checkout:

- the helper copies `.env.compose.example` into `.env.compose` if no compose env exists yet
- `.env.compose.example` is intentionally not launch-ready for live `ml_pure`
- `publish_runtime_config.sh` rejects that non-live `ml_pure` config by design

After bootstrap, edit `.env.compose` into a valid runtime config and then publish it explicitly.

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
2. install `ml_pipeline_2` and the required LightGBM / XGBoost Python packages
3. auto-install `libgomp1` on Ubuntu when `AUTO_INSTALL_SYSTEM_PACKAGES=1` and LightGBM is present but not runnable
4. verify the local `stage2_direction_view` schema before any training run
5. run the staged 1/2/3 release flow
6. if the release is `PUBLISH`, apply the generated `ML_PURE_*` handoff into `.env.compose`
7. if the release is `PUBLISH`, republish the runtime config bundle
8. if the release is `HOLD`, exit cleanly and print the blocking reasons

The wrapper is HOLD-safe: if the staged run fails a gate, it still writes the staged summary and release assessment artifacts, exits cleanly, and does not create `release/ml_pure_runtime.env`. That is the expected outcome for a valid HOLD.

Example:

```bash
tmux new -s training
```

Inside the `tmux` session:

```bash
./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release.log
```

For VM execution, run this inside `tmux` so the staged release survives SSH disconnects.

### `apply_ml_pure_release.sh`

Use this when you already have a `release/ml_pure_runtime.env` file and only want to update the handoff keys in `.env.compose`.

It updates:

- `STRATEGY_ENGINE`
- `ML_PURE_RUN_ID`
- `ML_PURE_MODEL_GROUP`

It does not set the capped-live guard file, rollout stage, position-size cap, or monitoring artifact env vars. Those must already be correct in `.env.compose` before `publish_runtime_config.sh` will accept the runtime config.

### `build_runtime_images.sh`

Builds and pushes runtime images to Artifact Registry.

### `publish_runtime_config.sh`

Uploads `.env.compose` and optional ingestion credentials to the runtime config GCS prefix.

### `publish_published_models.sh`

Syncs the local `published_models` tree to the model bucket.

### `publish_raw_market_data.sh`

Syncs a local raw `banknifty_data` archive to a GCS prefix such as `RAW_ARCHIVE_BUCKET_URL`.

Use this when you want to seed or refresh the canonical raw archive in GCS outside the main snapshot build script.
The historical parquet operator flow can also do this upload automatically when `LOCAL_RAW_ARCHIVE_ROOT` is set.

### `run_snapshot_parquet_pipeline.sh`

This is the only supported operator entrypoint for historical parquet creation and publish.

It will:

1. optionally upload a local raw `banknifty_data` archive to `RAW_ARCHIVE_BUCKET_URL`
2. sync the raw archive from GCS into `RAW_DATA_ROOT`
3. create or reuse `.venv`
4. normalize raw futures/options/spot/VIX into local parquet cache
5. audit source coverage vs built coverage
6. build only pending snapshot and derived parquet days with resume enabled by default
7. write build and validation reports, including `coverage_audit.json`
8. clean the stable GCS publish prefixes by default
9. publish final parquet and reports to `SNAPSHOT_PARQUET_BUCKET_URL`
10. verify the published GCS layout

If normalization fails partway through, the supported recovery path is to delete the local parquet cache, rerun with low worker counts, and let the wrapper rebuild from the canonical raw archive. A practical first retry is `NO_RESUME=1 NORMALIZE_JOBS=1 SNAPSHOT_JOBS=2`; if that still fails, drop both worker counts to `1`.

Important runtime knobs:

- `LOCAL_RAW_ARCHIVE_ROOT`
- `RAW_ARCHIVE_BUCKET_URL`
- `SNAPSHOT_PARQUET_BUCKET_URL`
- `NORMALIZE_JOBS`
- `SNAPSHOT_JOBS`
- `STAGE2_REQUIRED_COLUMNS`
- `SNAPSHOT_SLICE_MONTHS`
- `SNAPSHOT_SLICE_WARMUP_DAYS`

Worker defaults are conservative by design: `NORMALIZE_JOBS=1`, `SNAPSHOT_JOBS=2`.
The current fast path uses chunked snapshot partitions with warmup continuity, not calendar-year-only workers.
The derived Stage 2 view must contain `pcr_change_5m`, `pcr_change_15m`, `atm_oi_ratio`, `near_atm_oi_ratio`, `atm_ce_oi`, and `atm_pe_oi` before training starts.
For VM execution, run this inside `tmux` so the build and publish survive SSH disconnects:

```bash
tmux new -s snapshot
```

Inside the `tmux` session:

```bash
./ops/gcp/run_snapshot_parquet_pipeline.sh 2>&1 | tee snapshot-run.log
```

### `publish_snapshot_parquet.sh`

Syncs local final parquet outputs to `SNAPSHOT_PARQUET_BUCKET_URL`.

By default it uploads:

- canonical `snapshots`
- `market_base`
- derived `snapshots_ml_flat`
- stage views
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

1. create or use a large disposable GCP VM
2. place the raw archive on that machine and set `LOCAL_RAW_ARCHIVE_ROOT`, or pre-seed `RAW_ARCHIVE_BUCKET_URL`
3. run `run_snapshot_parquet_pipeline.sh`
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

For the operator index, use [runbooks/README.md](../../docs/runbooks/README.md).
For historical parquet creation, use [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md).
For staged training and publish, use [TRAINING_RELEASE_RUNBOOK.md](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md).
For runtime deploy and cutover, use [GCP_DEPLOYMENT.md](../../docs/runbooks/GCP_DEPLOYMENT.md).
For cleanup and rollback, use [CLEANUP_ROLLBACK_RUNBOOK.md](../../docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md).
Use [../../ml_pipeline_2/docs/gcp_user_guide.md](../../ml_pipeline_2/docs/gcp_user_guide.md) only for package-level staged ML detail.

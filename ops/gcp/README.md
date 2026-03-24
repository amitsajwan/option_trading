# GCP Operator Scripts

These scripts are the runnable operator layer over Terraform, GCP, runtime deploy, and staged ML release flows.

Run them from:

- Ubuntu
- WSL
- Cloud Shell

They assume a Bash environment.

This file is a script index, not the primary operator runbook. For step-by-step procedures, use:

- [../../docs/runbooks/README.md](../../docs/runbooks/README.md)
- [../../docs/runbooks/GCP_DEPLOYMENT.md](../../docs/runbooks/GCP_DEPLOYMENT.md)
- [../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
- [../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [../../docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md](../../docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md)

## Current Entry Points

Primary menu:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Direct entrypoints:

- `bash ./ops/gcp/bootstrap_runtime_interactive.sh`
- `bash ./ops/gcp/start_runtime_interactive.sh`
- `bash ./ops/gcp/start_training_interactive.sh`
- `bash ./ops/gcp/run_snapshot_parquet_pipeline.sh`

## Operator Env

1. Copy [operator.env.example](operator.env.example) to `ops/gcp/operator.env`
2. Fill in the actual project, bucket, repo, and release values

Current bootstrap derives:

- `MODEL_BUCKET_URL=gs://<MODEL_BUCKET_NAME>/published_models`
- `RUNTIME_CONFIG_BUCKET_URL=gs://<RUNTIME_CONFIG_BUCKET_NAME>/runtime`

Runtime deployments currently use GHCR-published images. `REPOSITORY` remains in `operator.env` because the Terraform and bootstrap layer still carries Artifact Registry compatibility.

## Script Index

### `runtime_lifecycle_interactive.sh`

Single menu-driven entrypoint for daily operations:

1. bootstrap infra
2. start or restart runtime deploy
3. stop runtime VM
4. destroy infra and preserve data
5. start training

The menu invokes sub-scripts via `bash`, so it works even if execute bits are missing after clone.

### `bootstrap_runtime_interactive.sh`

Interactive setup for `ops/gcp/operator.env`.

Use it to collect the changing environment values, derive bucket URLs, and optionally run bootstrap immediately.

### `from_scratch_bootstrap.sh`

Use this from a fresh operator machine or repo checkout to:

1. write `infra/gcp/terraform.tfvars`
2. run Terraform
3. optionally build runtime images
4. optionally publish the runtime config bundle

Use `RUN_RUNTIME_CONFIG_SYNC=0` on a fresh checkout when `.env.compose` is still a template.

### `start_runtime_interactive.sh`

Interactive runtime deploy helper.

It validates runtime inputs, optionally runs Kite browser auth, updates `.env.compose`, publishes runtime config, and then prompts for a VM start or restart action.

### `create_training_vm.sh`

Create a disposable training VM from the Terraform output instance template.

### `delete_training_vm.sh`

Delete the disposable training VM when training is complete.

### `start_training_interactive.sh`

Interactive training launcher for full publish, quick test, HPO, diagnostic, and grid modes.

It writes logs and release payloads under:

- `ml_pipeline_2/artifacts/training_launches/<utc_stamp>_<nonce>_<mode>_<lane_tag>_<model_group>_<profile_id>/`

### `run_staged_release_pipeline.sh`

Low-level staged release wrapper used by manual and interactive training flows.

It is HOLD-safe and writes release assessment artifacts even when the candidate is rejected.

### `run_snapshot_parquet_pipeline.sh`

Supported operator entrypoint for historical parquet creation and publish.

### `publish_runtime_config.sh`

Upload `.env.compose`, optional ingestion credentials, runtime guard, and referenced runtime artifacts to the runtime config bucket prefix.

### `apply_ml_pure_release.sh`

Apply the staged release handoff keys into `.env.compose`.

### `build_runtime_images.sh`

Build and push runtime images to Artifact Registry.

This is currently an infra-compatibility path, not the primary live runtime deploy path. Live runtime deploys use GHCR image tags configured in `.env.compose` and `operator.env`.

### `stop_runtime.sh`

Stop the always-on runtime VM without deleting persistent resources.

### `destroy_infra_preserve_data.sh`

Destroy Terraform-managed compute, networking, and IAM resources while preserving deployable data state.

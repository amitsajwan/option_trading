# GCP Operator Scripts

These scripts are the runnable operator layer over Terraform, GCP, runtime deploy, and staged ML release flows.

**First time or rebuilding from scratch?** Start with the sequential setup guide, not this index:

- [../../docs/runbooks/LIVE_SETUP_GUIDE.md](../../docs/runbooks/LIVE_SETUP_GUIDE.md) — Zero-to-live guide: prerequisites → infra → parquet → training → live deploy → daily ops.

Run scripts from:

- Ubuntu
- WSL
- Cloud Shell

They assume a Bash environment.

Important host rule:

- `run_snapshot_parquet_pipeline.sh` is Linux-only
- use Windows only for raw archive upload via `publish_raw_market_data.sh`
- use Cloud Shell for orchestration, not as the default full parquet build host
- use a large-disk Linux VM for snapshot/parquet builds

This file is a script index, not the primary operator runbook. For step-by-step procedures, use:

- [../../docs/runbooks/LIVE_SETUP_GUIDE.md](../../docs/runbooks/LIVE_SETUP_GUIDE.md)
- [../../docs/runbooks/README.md](../../docs/runbooks/README.md)
- [../../docs/runbooks/GCP_DEPLOYMENT.md](../../docs/runbooks/GCP_DEPLOYMENT.md)
- [../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
- [../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [../../docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md](../../docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md)

GCP historical replay uses the same GHCR image tags and Compose overlays as the live runtime, but it is a separate operator lane. Build and publish parquet first with the snapshot runbook, then sync parquet onto the replay VM or runtime VM before starting the `historical` and `historical_replay` Compose profiles.

Preferred operator shape:

- `Infra`
- `Live`
- `Historical`

Image source modes:

- `IMAGE_SOURCE=ghcr` uses published GHCR images through `docker-compose.gcp.yml`
- `IMAGE_SOURCE=local_build` builds from the repo checkout on the VM with `docker-compose.yml`

Use the interactive lifecycle menu as the primary entrypoint for `Infra`, `Live`, and `Historical replay`.

Snapshot/parquet build is separate. Its supported entrypoint is:

```bash
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Run that wrapper on a dedicated Linux snapshot-build host with large local disk.

## Recommended Operator Flow

Use this menu first:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

That is the primary operator path for:

- menu item `1`: `Infra`
- menu item `2`: `Live`
- menu item `3`: `Historical replay`

Important boundary:

- the menu is the preferred path for infra, live runtime work, and historical replay
- the live and historical branches remain separate inside that menu because they prepare different runtime inputs

## Script Entry Points

Use direct entrypoints only when you intentionally need a lower-level or troubleshooting path. The lifecycle menu remains the supported operator entrypoint for first-time and daily use across `Infra`, `Live`, and `Historical replay`; snapshot/parquet build remains a separate supported wrapper.

Primary menu:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Direct entrypoints:

- `bash ./ops/gcp/bootstrap_runtime_interactive.sh`
- `bash ./ops/gcp/start_runtime_interactive.sh`
- `bash ./ops/gcp/start_historical_interactive.sh`
- `bash ./ops/gcp/start_training_interactive.sh`
- `bash ./ops/gcp/run_snapshot_parquet_pipeline.sh`

## Operator Env

1. Copy [operator.env.example](operator.env.example) to `ops/gcp/operator.env`
2. Fill in the actual project, bucket, repo, and release values

**Quick setup for the `amittrading-493606` project:**

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
python3 ops/gcp/patch_operator_env.py
```

`patch_operator_env.py` replaces all template placeholders (`my-gcp-project`,
`my-option-trading-models`, etc.) with the real `amittrading-493606` project values.
Run it once after a fresh clone or VM rebuild. Safe to re-run.

Current bootstrap derives:

- `MODEL_BUCKET_URL=gs://<MODEL_BUCKET_NAME>/published_models`
- `RUNTIME_CONFIG_BUCKET_URL=gs://<RUNTIME_CONFIG_BUCKET_NAME>/runtime`

Runtime deployments default to GHCR-published images, but both live and historical helpers now also support `IMAGE_SOURCE=local_build`. `REPOSITORY` remains in `operator.env` because the Terraform and bootstrap layer still carries Artifact Registry compatibility.

## Script Index

### `runtime_lifecycle_interactive.sh`

Single menu-driven entrypoint for daily operations:

1. bootstrap infra
2. start or restart runtime deploy
3. historical replay
4. stop runtime VM
5. destroy infra and preserve data
6. start training

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

It auto-fetches the current approved runtime release artifacts from the runtime-config bucket, applies the runtime handoff into `.env.compose`, prompts for `IMAGE_SOURCE`, shows Kite credential state, runs shared live preflight, publishes runtime config, and then prompts for a VM start or restart action.

### `start_historical_interactive.sh`

Interactive historical replay helper.

It defaults to the runtime VM, prompts for `IMAGE_SOURCE`, uses the current approved image tag when GHCR is selected, detects the repo checkout path and Compose implementation on the target VM, syncs the runtime bundle when needed, runs local and remote historical preflight, then can optionally sync parquet, build historical services from code, start historical services, and run one-shot replay.

For `ml_pure` replay it now also:

- clears explicit model-package / threshold-path conflicts when run-id mode is active
- force-writes the historical `ml_pure` overrides on each run: `STRATEGY_ROLLOUT_STAGE_HISTORICAL=capped_live`, `STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL=0.25`, and `ML_PURE_MAX_FEATURE_AGE_SEC_HISTORICAL=0`
- uses a dedicated historical test guard at `.run/ml_runtime_guard_historical_test.json` instead of reusing the live runtime guard
- requires a `strategy_app` image built with `scikit-learn==1.7.2` for the current staged smoke bundles

### `run_historical_replay_shell.sh`

Non-interactive shell runner for historical replay.

Use this when you already know the target VM, replay dates, parquet bucket, and model switch inputs. It enforces the remote historical `ml_pure` env, writes a dedicated historical test guard on the target VM, syncs parquet and published model artifacts, restarts the historical services, and runs one-shot replay in one shell command flow.

It waits for `strategy_app_historical` and `strategy_persistence_app_historical` to subscribe before the replay run starts, so fast one-shot replays do not outrun the consumers.

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

It is HOLD-safe and writes release assessment artifacts even when the candidate is rejected. On `PUBLISH`, it also writes the runtime release manifest/current-pointer artifacts used by the live deploy flow.

### `operator_preflight.py`

Shared preflight validator for `infra`, `live`, and `historical` operator lanes.

### `runtime_release_manifest.py`

Runtime release manifest writer used by the training publish lane and consumed by live deploy.

### `run_snapshot_parquet_pipeline.sh`

Supported operator entrypoint for historical parquet creation and publish.

This is the upstream artifact-build step for both training and GCP historical simulation. It does not start replay services on the runtime VM.

Use it for:

- fresh raw-to-parquet rebuilds
- resumable reruns on the snapshot-build host
- targeted year or date-window rebuilds
- publish of the canonical parquet artifact set consumed by training and historical replay

Operational rules:

- Linux only
- prefer a dedicated build VM over Cloud Shell or the runtime VM
- start with its host-aware worker defaults before overriding `NORMALIZE_JOBS` or `SNAPSHOT_JOBS`
- run it inside `tmux` and treat rerunning the same command as the normal restart path

### `publish_runtime_config.sh`

Upload `.env.compose`, optional ingestion credentials, runtime guard, and referenced runtime artifacts to the runtime config bucket prefix.

Keep this bundle small. Do not use it to ship historical parquet datasets.

### `patch_operator_env.py`

Replace `operator.env` template placeholders with real `amittrading-493606` project values.

Run once after a fresh clone or VM rebuild:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
python3 ops/gcp/patch_operator_env.py
```

### `force_deploy_research_run.sh`

Force-deploy a completed research run to the live runtime, bypassing automated hard gate failures.

Use when a run returns HOLD due to combined gate failures (e.g. TRENDING regime drag) but has demonstrable edge in specific regimes (e.g. VOLATILE PF > 1.3).

Steps it automates:
1. Force-publish local model bundle
2. Write `release/ml_pure_runtime.env`
3. Build a `force_training_release.json` compatible with `runtime_release_manifest.py`
4. Write `.run/gcp_release/current_runtime_release.json`
5. Sync published bundle to GCS model bucket
6. Publish runtime config bundle to GCS runtime-config bucket

Run on the training VM. Then `start_runtime_interactive.sh` from the operator machine.

See [TRAINING_RELEASE_RUNBOOK.md — Force-Deploying a Research Run](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md) for full usage and rollback instructions.

### `apply_ml_pure_release.sh`

Apply the staged release handoff keys into `.env.compose`.

### `build_runtime_images.sh`

Build and push runtime images to Artifact Registry.

This is currently an infra-compatibility path, not the primary live runtime deploy path. Live runtime deploys use GHCR image tags configured in `.env.compose` and `operator.env`.

### `stop_runtime.sh`

Stop the always-on runtime VM without deleting persistent resources.

### `destroy_infra_preserve_data.sh`

Destroy Terraform-managed compute, networking, and IAM resources while preserving deployable data state.

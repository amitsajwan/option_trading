# Runbooks Index

Start here if you are operating the current GCP workflow.

## Current Operator Entry Point

For day-to-day runtime and training operations, start with:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

That menu is the supported operator entrypoint for:

1. infra bootstrap
2. runtime deploy and restart
3. historical replay
4. runtime stop
5. preserve-data teardown
6. interactive training launch

Use the runbooks below when you need the detailed step-by-step flow, validation steps, or recovery guidance behind that menu.

For release management, treat the GCP deployment flow in this conceptual order:

- `Infra`
- `Live`
- `Historical`

In the actual lifecycle menu these map to:

- menu item `1`: infra bootstrap
- menu item `2`: live deploy/restart
- menu item `3`: historical replay

## Primary Runbooks

1. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
   Historical snapshot and parquet creation.
2. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md)
   Staged ML training, research sequencing, publish, and runtime handoff generation.
3. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)
   Release-manager runbook for `0. Infra`, `1. Live`, and `2. Historical`, centered on the interactive GCP operator flow, shared preflight, and current approved runtime release artifacts.

## Supporting Runbook

4. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)
   Stop spend, remove temporary compute, or roll back runtime config.

## Scope Notes

- Runtime deployment defaults to GHCR-published images, with `IMAGE_SOURCE=local_build` available for faster code-to-runtime iteration.
- Training and runtime both depend on `ops/gcp/operator.env`.
- Artifact Registry still exists in Terraform and bootstrap flows for infra compatibility, but it is not the primary runtime image source.

Read [../SYSTEM_SOURCE_OF_TRUTH.md](../SYSTEM_SOURCE_OF_TRUTH.md) first if you need the current non-negotiable runtime and training rules.

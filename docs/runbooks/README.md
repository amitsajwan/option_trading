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
3. runtime stop
4. preserve-data teardown
5. interactive training launch

Use the runbooks below when you need the detailed step-by-step flow, validation steps, or recovery guidance behind that menu.

## Primary Runbooks

1. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
   Historical snapshot and parquet creation.
2. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md)
   Staged ML training, research sequencing, publish, and runtime handoff generation.
3. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)
   Live runtime deployment, runtime config publish, VM restart, validation, and rollback.

## Supporting Runbook

4. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)
   Stop spend, remove temporary compute, or roll back runtime config.

## Scope Notes

- Runtime deployment currently uses GHCR-published images for the live stack.
- Training and runtime both depend on `ops/gcp/operator.env`.
- Artifact Registry still exists in Terraform and bootstrap flows for infra compatibility, but it is not the primary runtime image source.

Read [../SYSTEM_SOURCE_OF_TRUTH.md](../SYSTEM_SOURCE_OF_TRUTH.md) first if you need the current non-negotiable runtime and training rules.

# Runbooks Index

## Start Here

**First time or rebuilding from scratch?** Read this first:

- [LIVE_SETUP_GUIDE.md](LIVE_SETUP_GUIDE.md) — Complete zero-to-live sequential guide.
  Covers prerequisites, environment files, infra bootstrap, parquet build, smoke training, historical replay, production training, Kite auth, live deploy, daily operations, and rollback. All phases in dependency order.

---

## Current Operator Entry Point

For day-to-day runtime, historical replay, and training operations, start with:

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

Snapshot/parquet build is the one major operator flow that stays outside that menu. Its supported entrypoint is:

```bash
bash ./ops/gcp/run_snapshot_parquet_pipeline.sh
```

Run that wrapper on a dedicated Linux snapshot-build host, not on Cloud Shell as the default full-build host.

## Fresh Rebuild Order

When the project is new or the derived buckets are empty, use this exact sequence:

1. `Infra`
2. raw archive upload to GCS
3. snapshot/parquet build and publish on a dedicated Linux build host
4. smoke training publish
5. historical replay validation
6. production training and publish
7. `Live`

Keep this dependency rule hard:

- parquet before training
- smoke publish before production research
- historical validation before live deploy

For release management, treat the GCP deployment flow in this conceptual order:

- `Infra`
- `Live`
- `Historical`

In the actual lifecycle menu these map to:

- menu item `1`: infra bootstrap
- menu item `2`: live deploy/restart
- menu item `3`: historical replay

## Primary Runbooks

1. [LIVE_SETUP_GUIDE.md](LIVE_SETUP_GUIDE.md)
   Zero-to-live sequential guide. Read this first on a new setup.
2. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
   Historical snapshot/parquet build and publish, including dedicated-host requirements, worker tuning, and restart guidance.
3. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md)
   Staged ML training, research sequencing, publish, and runtime handoff generation.
4. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)
   Release-manager runbook for `0. Infra`, `1. Live`, and `2. Historical`, centered on the interactive GCP operator flow, shared preflight, and current approved runtime release artifacts.

## Supporting Runbooks

5. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)
   Stop spend, remove temporary compute, or roll back runtime config.
6. [DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md](DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md)
   Local/operator runbook for full historical deterministic replay with dashboard, on-demand date windows, and fresh image rebuilds from current code.

## Scope Notes

- Runtime deployment defaults to GHCR-published images, with `IMAGE_SOURCE=local_build` available for faster code-to-runtime iteration.
- Training and runtime both depend on `ops/gcp/operator.env`.
- Artifact Registry still exists in Terraform and bootstrap flows for infra compatibility, but it is not the primary runtime image source.

Read [../SYSTEM_SOURCE_OF_TRUTH.md](../SYSTEM_SOURCE_OF_TRUTH.md) first if you need the current non-negotiable runtime and training rules.

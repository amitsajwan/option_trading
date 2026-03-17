# Operator Runbook Index

This is the start-here document for a release manager or operator.

Do not treat this file as the detailed procedure itself. Its job is to route you to the correct runbook.

## Audience

Use this if you are:

- bootstrapping a new GCP environment
- building historical snapshot parquet
- running a training/release cycle
- deploying or cutting over runtime
- stopping, rolling back, or cleaning up resources

## Choose The Right Runbook

### No GCP resources exist yet

Use [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md).

This covers:

- GCP project and APIs
- `ops/gcp/operator.env`
- Terraform apply
- runtime/training base infrastructure
- first image and runtime-config bootstrap

### Need final historical parquet from raw data

Use [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md).

This covers:

- raw archive upload
- high-power build VM
- final canonical `snapshots`
- derived `snapshots_ml_flat`
- GCS upload of parquet and reports

### Need to train and publish a model release

Use [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md).

This covers:

- training VM lifecycle
- guarded `ml_pipeline_2` release flow
- published model sync
- runtime handoff generation

### Need to deploy or switch live runtime

Use [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md).

This covers:

- runtime image build/push
- runtime config publish
- runtime VM restart or recreate
- validation and rollback

### Need full stack or Docker bring-up

Use [SUPPORT_BRINGUP_GUIDE.md](SUPPORT_BRINGUP_GUIDE.md).

This covers:

- Compose stack bring-up
- `snapshot_app`
- `strategy_app`
- persistence and dashboard checks

### Need stop, rollback, or cleanup

Use [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md).

This covers:

- stop runtime
- delete training VM
- preserve-data teardown
- full destroy
- rollback choices

## Standard Sequences

### Day 0 full setup

1. [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md)
2. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) if historical parquet is required
3. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md) if you need a fresh model release
4. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)

### Normal recurring release

1. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md) if model artifacts need to change
2. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)
3. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md) when done

### Data-only rebuild

1. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
2. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)

## One-Time Vs Recurring

### One-time or rare

- project setup
- API enablement
- Terraform base infra creation
- snapshot storage bucket setup
- first runtime bootstrap

### Recurring

- snapshot parquet rebuild
- training/release runs
- runtime deploys
- stop/rollback/cleanup

## Supporting References

These are reference docs, not the primary operator procedure:

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md)
- [strategy_eval_architecture.md](strategy_eval_architecture.md)

## Script Layer

Runnable wrappers live in [ops/gcp/README.md](../ops/gcp/README.md).

The runbooks above should point to exact commands, while `ops/gcp` remains the execution layer used by both humans and future GitHub Actions.

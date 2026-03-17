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

## First Commands In Cloud Shell

If you are starting on a fresh Cloud Shell session, begin here:

```bash
gcloud config set project <project-id>
git clone <repo-clone-url>
cd option_trading
git checkout <repo-ref>
git pull --ff-only
```

For this repo, the concrete values usually come from `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REPO_CLONE_URL`
- `REPO_REF`

After the checkout is ready, continue with the runbook lane below.

## Choose The Right Runbook

### No GCP resources exist yet

Use [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md).

This covers:

- GCP project and APIs
- `ops/gcp/operator.env`
- Terraform apply
- runtime/training base infrastructure
- first image and runtime-config bootstrap

This is the full-platform lane. It creates more than the snapshot-only lane and more than day-to-day training needs.

### Need final historical parquet from raw data

Use [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md).

This covers:

- minimal snapshot-only GCP setup
- raw archive upload
- high-power build VM
- final canonical `snapshots`
- derived `snapshots_ml_flat`
- GCS upload of parquet and reports

This lane does not require:

- Artifact Registry
- model bucket
- runtime-config bucket
- runtime VM
- training VM template
- runtime image build
- runtime config publish
- keeping the runtime VM running
- Terraform

### Need to train and publish a model release

Use [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md).

This covers:

- training VM lifecycle
- guarded `ml_pipeline_2` release flow
- published model sync
- runtime handoff generation

This lane does not require:

- runtime image build
- a running runtime VM
- a snapshot-build VM

### Need to deploy or switch live runtime

Use [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md).

This covers:

- runtime image build/push
- runtime config publish
- runtime VM restart or recreate
- validation and rollback

This lane does not require:

- a running training VM
- a snapshot-build VM

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

### Day 0 full runtime-capable setup

1. [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md)
2. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) if historical parquet is required
3. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md) if you need a fresh model release
4. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)

### Day 0 snapshot-only setup

1. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
2. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md) if you want to delete the temporary snapshot VM after upload

This path is `gcloud`-only.

## Resources By Phase

### Snapshot-only phase

Create only:

- snapshot data bucket
- one temporary snapshot-build VM

Avoid creating:

- runtime VM
- training VM template
- model bucket
- runtime-config bucket
- Artifact Registry

### Training/release phase

Use or keep:

- training VM template
- one disposable training VM
- model bucket
- runtime-config bucket

Optional:

- runtime VM can stay stopped until deploy time

Not required for training itself:

- runtime image build
- running runtime VM
- snapshot-build VM

### Runtime/deploy phase

Use or keep:

- runtime VM
- Artifact Registry
- runtime-config bucket
- model bucket

Not required:

- training VM after publish
- snapshot-build VM

### Full bootstrap phase

Creates the shared runtime/training platform:

- runtime VM
- training VM template
- model bucket
- runtime-config bucket
- Artifact Registry
- IAM, firewall, static IP

Use this only when you actually want the full platform in place.

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
- Terraform base infra creation for runtime/training lanes
- snapshot storage bucket setup for the snapshot lane
- first runtime bootstrap for runtime/deploy lanes

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

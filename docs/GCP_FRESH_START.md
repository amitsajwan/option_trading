# GCP Fresh Start

This document is now a short orientation note, not the primary operator procedure.

Use it when you want the high-level shape of the recommended GCP operating model:

- small runtime VM
- disposable training VM
- shared GCS buckets for deployable state
- reproducible Terraform-managed infra
- optional disposable high-power VM for historical parquet builds

## Recommended Operating Model

- keep runtime small
- create training VMs only when needed
- keep published models and runtime config in GCS
- keep runtime images in Artifact Registry
- rebuild compute from Terraform instead of treating VMs as pets

## Use These Runbooks

If you have no usable GCP resources yet:

- [GCP_BOOTSTRAP_RUNBOOK.md](GCP_BOOTSTRAP_RUNBOOK.md)

If you need final historical parquet from raw archive:

- [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)

If you need to train and publish a new model release:

- [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md)

If you need to deploy or cut over runtime:

- [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)

If you need to stop or tear down resources:

- [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)

If you want the operator index:

- [FROM_SCRATCH_OPERATOR_GUIDE.md](FROM_SCRATCH_OPERATOR_GUIDE.md)

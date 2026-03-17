# GCP Terraform Scaffold

This folder contains a first-pass Terraform scaffold for the deployment model used by this repo:

- one small runtime VM
- one disposable training VM template
- Artifact Registry for runtime images
- Cloud Storage for published models and runtime bootstrap files
- optional Cloud Storage bucket for raw archive and final historical parquet

## What It Creates

- Docker Artifact Registry repository
- model artifact bucket with versioning enabled
- runtime config bucket
- optional snapshot data bucket
- runtime service account
- training service account
- runtime static IP
- firewall rules for SSH and dashboard access
- runtime VM with a startup script
- training VM instance template with a bootstrap script

## Files

- [versions.tf](versions.tf)
- [variables.tf](variables.tf)
- [locals.tf](locals.tf)
- [artifact_registry.tf](artifact_registry.tf)
- [storage.tf](storage.tf)
- [iam.tf](iam.tf)
- [networking.tf](networking.tf)
- [compute.tf](compute.tf)
- [outputs.tf](outputs.tf)
- [terraform.tfvars.example](terraform.tfvars.example)

## Quick Start

```bash
cd infra/gcp
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

## Expected Workflow

1. Apply Terraform once for the base infra.
2. Build and push runtime images with [ops/gcp/build_runtime_images.sh](../../ops/gcp/build_runtime_images.sh).
3. Upload `.env.compose` and `ingestion_app/credentials.json` with [ops/gcp/publish_runtime_config.sh](../../ops/gcp/publish_runtime_config.sh).
4. Publish and sync models with [ops/gcp/publish_published_models.sh](../../ops/gcp/publish_published_models.sh).
5. Let the runtime VM startup script sync artifacts and start Compose.
6. Create a large training VM only when needed from the output training template.
7. Optionally use the snapshot data bucket for raw archive and final historical parquet artifacts.

If you want the runnable operator scripts that sit on top of this scaffold, use [ops/gcp/README.md](../../ops/gcp/README.md).
If you want the human-facing Day 0 procedure, use [GCP_BOOTSTRAP_RUNBOOK.md](../../docs/GCP_BOOTSTRAP_RUNBOOK.md).

## Notes

- The scaffold intentionally keeps runtime and training separate.
- The training template prepares the repo and Python environment but does not automatically start any experiment.
- The runtime startup script is opinionated around Docker Compose because that is the preferred runtime path in this repo.

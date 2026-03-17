# GCP Bootstrap Runbook

Use this when no usable GCP environment exists yet for this repo.

This is the Day 0 operator procedure for:

- project and API readiness
- `ops/gcp/operator.env`
- Terraform-managed base infrastructure
- first runtime image push
- first runtime config publish

If you only need historical parquet, training, or runtime cutover after bootstrap, return to [FROM_SCRATCH_OPERATOR_GUIDE.md](FROM_SCRATCH_OPERATOR_GUIDE.md) and choose the narrower runbook.

## Audience

- release manager
- platform operator
- ML operator doing first setup in a new project

## Preconditions

- GCP project exists and billing is enabled
- you can use Cloud Shell or another Bash environment with `gcloud`, `git`, and `terraform`
- repo is checked out on the target operator machine
- you know the repo branch or tag you want VMs to use

## Inputs To Fill

Copy [operator.env.example](../ops/gcp/operator.env.example) to `ops/gcp/operator.env` and fill at least:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `REPO_CLONE_URL`
- `REPO_REF`
- `RUNTIME_MACHINE_TYPE`
- `TRAINING_MACHINE_TYPE`
- `MODEL_BUCKET_NAME`
- `RUNTIME_CONFIG_BUCKET_NAME`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`
- `DATA_SYNC_SOURCE`

If you also want a shared bucket for raw archive and final historical parquet, fill:

- `SNAPSHOT_DATA_BUCKET_NAME`
- `RAW_ARCHIVE_BUCKET_URL`
- `SNAPSHOT_PARQUET_BUCKET_URL`

## Terraform Layout

Terraform is now intentionally split by concern instead of one large file:

- [versions.tf](../infra/gcp/versions.tf)
- [variables.tf](../infra/gcp/variables.tf)
- [locals.tf](../infra/gcp/locals.tf)
- [artifact_registry.tf](../infra/gcp/artifact_registry.tf)
- [storage.tf](../infra/gcp/storage.tf)
- [iam.tf](../infra/gcp/iam.tf)
- [networking.tf](../infra/gcp/networking.tf)
- [compute.tf](../infra/gcp/compute.tf)
- [outputs.tf](../infra/gcp/outputs.tf)

Yes, this is the right shape for the repo now. Runtime, storage, IAM, and networking are easier to review and operate separately than they were in the old monolithic `main.tf`.

## APIs To Enable

At minimum, make sure these are enabled:

- `compute.googleapis.com`
- `artifactregistry.googleapis.com`
- `cloudbuild.googleapis.com`
- `storage.googleapis.com`
- `iamcredentials.googleapis.com`
- `cloudresourcemanager.googleapis.com`

Example:

```bash
gcloud services enable \
  compute.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com
```

## Bootstrap Commands

From repo root:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

Edit `ops/gcp/operator.env`, then run:

```bash
./ops/gcp/from_scratch_bootstrap.sh
```

What this does:

1. writes `infra/gcp/terraform.tfvars`
2. runs `terraform init/plan/apply`
3. builds and pushes runtime images
4. publishes the runtime bootstrap bundle to GCS

Useful variants:

Skip image rebuild:

```bash
RUN_IMAGE_BUILD=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Skip runtime config publish:

```bash
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Apply Terraform non-interactively:

```bash
TERRAFORM_AUTO_APPROVE=1 ./ops/gcp/from_scratch_bootstrap.sh
```

## Expected Result

After a successful bootstrap you should have:

- Artifact Registry repository
- model bucket
- runtime config bucket
- optional snapshot data bucket
- runtime VM
- training instance template
- runtime and training service accounts
- firewall rules
- runtime static IP

## Validation

Check Terraform outputs:

```bash
cd infra/gcp
terraform output
```

Check runtime VM:

```bash
gcloud compute instances describe option-trading-runtime --zone "${ZONE}"
```

Check buckets:

```bash
gcloud storage ls "gs://${MODEL_BUCKET_NAME}"
gcloud storage ls "gs://${RUNTIME_CONFIG_BUCKET_NAME}"
```

If snapshot storage was requested:

```bash
gcloud storage ls "gs://${SNAPSHOT_DATA_BUCKET_NAME}"
```

## Next Runbook

Choose the next lane:

- build historical parquet: [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- train and publish a model: [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md)
- deploy or cut over runtime: [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)
- stop or tear down resources: [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)


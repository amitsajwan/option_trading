# From Scratch Operator Guide

This is the single end-to-end guide for rebuilding the supported runtime and training workflow from scratch on GCP.

Use this when:

- the old VM setup is messy or too expensive
- you want a clean Terraform-based rebuild
- you want runtime and training separated properly
- you want one repeatable path for training, publishing, and switching live runtime

At the end of this guide you will have:

- one small always-on runtime VM
- one disposable training VM template
- Artifact Registry for container images
- Cloud Storage for published models and runtime bootstrap files
- one supported ML release path from training to live runtime
- one clear split between GitHub Actions automation and the small number of manual operator steps that still remain

Runnable helper scripts live under [ops/gcp/README.md](/c:/code/option_trading/ops/gcp/README.md).

## 1. Target Shape

Use this operating model:

- runtime VM:
  - small
  - always on
  - runs Docker Compose only
- training VM:
  - created only when needed
  - used for research, threshold sweep, and release
  - deleted after work is done
- source of truth:
  - code images: Artifact Registry
  - published models: Cloud Storage
  - runtime bootstrap config: Cloud Storage
  - infrastructure: Terraform
  - deployment orchestration: GitHub Actions

Do not go back to host-side `nohup python ...` processes on public ports.

## 2. Recommended Control Plane

Use GitHub Actions as the control plane and GCP as the execution environment.

That means:

- GitHub Actions should run CI, image builds, Terraform plan/apply, and release orchestration
- GCP should run the actual runtime VM and the actual training VM
- heavy model training should not run on a normal GitHub-hosted runner
- the checked-in Bash scripts under `ops/gcp` should remain the executable building blocks, whether a human runs them from Cloud Shell/WSL or GitHub Actions runs them in automation

Recommended workflow split:

- `ci`
  - run tests and validation on pull requests
- `images`
  - build and push runtime images to Artifact Registry on merge to `main`
- `infra`
  - run Terraform plan/apply with approval
- `train-release`
  - create a disposable training VM
  - run the guarded `ml_pipeline_2.run_recovery_release` flow
  - sync published model artifacts to GCS
  - refresh the runtime config bundle
- `deploy-runtime`
  - restart or recreate the runtime VM so it pulls the latest config, models, and images

## 3. Will This Work?

Yes, this deployment shape should work reliably if the one-time setup below is done correctly.

The checked-in repo pieces already cover:

- Terraform-based infra creation
- runtime VM bootstrap
- disposable training VM creation
- image build/push scripts
- guarded train/sweep/publish/sync flow
- runtime handoff via `ML_PURE_*`

What is still required is disciplined setup around GitHub and GCP:

- the repo must be hosted in GitHub
- GitHub Actions must be enabled for the repo
- GCP APIs must be enabled
- Workload Identity Federation between GitHub and GCP must be configured
- GitHub repository or environment secrets/variables must be set
- `.env.compose` and any ingestion credentials must be valid

If those pieces are in place, the operational model is sound:

- merge code
- build images
- provision or update infra
- run a release
- switch runtime

## 4. What Must Be Done Manually Once

These are the main one-time manual tasks. They are normal and expected even in a mature setup.

### GitHub repository setup

Do these once:

- create or confirm the GitHub repo
- enable GitHub Actions
- create protected environments such as `staging` and `production`
- configure branch protection for `main`

### GCP project setup

Do these once:

- create or select the GCP project
- enable required APIs:
  - Compute Engine
  - Artifact Registry
  - Cloud Build
  - Cloud Storage
  - IAM Credentials API
- choose region and zone defaults

### GitHub to GCP authentication

Do these once:

- create a Workload Identity Pool and Provider in GCP
- create the GitHub-deploy service account(s)
- grant the required IAM roles
- allow the GitHub repository to impersonate those service accounts

Recommended minimum split:

- one deploy identity for image build and runtime deploy
- one infra identity for Terraform

### Repository configuration

Do these once:

- create GitHub repository variables for non-secret values such as project ID, region, repository name, bucket names, runtime name, and image tag strategy
- create GitHub secrets only for values that are actually secret
- keep long-lived JSON keys out of GitHub if possible

### Baseline runtime config

Do these once before the first deploy:

- prepare `.env.compose`
- verify deterministic runtime works first
- add ingestion credentials if that service is required

## 5. What Stays Manual Even After Automation

Even with GitHub Actions, some steps should remain manual on purpose:

- approving Terraform apply into production
- approving production runtime deploys
- deciding when to run a training release
- reviewing model quality before accepting a new live model
- emergency rollback decisions

This is good. Those are change-control points, not automation failures.

## 6. What Must Be Installed Where

### Operator machine

Install these manually:

- `gcloud`
- `terraform`
- `git`
- optional: Docker for local Compose checks
- optional but recommended: `gh` CLI
- a Bash-capable shell such as Ubuntu, WSL, or Cloud Shell

### Runtime VM

Installed automatically by the runtime startup script:

- Docker
- Docker Compose plugin
- Git
- Google Cloud CLI

### Training VM

Installed automatically by the training startup script:

- Python 3
- `python3-venv`
- `python3-pip`
- `gcc`
- `g++`
- `libgomp1`
- Google Cloud CLI

## 7. Cost Decision First

If the current 32-core training VM does not contain anything you still need locally:

1. stop it
2. confirm you do not need any unpublished files under:
   - `ml_pipeline_2/artifacts/research`
   - `ml_pipeline_2/artifacts/research_matrices`
   - `ml_pipeline_2/artifacts/published_models`
3. delete it

If you are unsure, export anything important to GCS first.

## 8. Recommended Starting Sizes

Start small and scale only if needed:

- runtime VM: `e2-standard-4`
- training VM template: `n2-standard-8`

Those recommended example sizes are reflected in [terraform.tfvars.example](/c:/code/option_trading/infra/gcp/terraform.tfvars.example).

## 9. Prerequisites

Before provisioning, make sure you have:

- a GCP project
- `gcloud` authenticated to that project
- `terraform` installed
- repo access for the clone URL you will put into Terraform
- a clean `.env.compose` ready for runtime bootstrap
- a Bash-capable operator shell such as Ubuntu, WSL, or Cloud Shell for the helper scripts
- if using GitHub Actions, the GitHub-to-GCP federation pieces from this guide already created

Useful repo entry points:

- [GCP_DEPLOYMENT.md](/c:/code/option_trading/docs/GCP_DEPLOYMENT.md)
- [GCP_FRESH_START.md](/c:/code/option_trading/docs/GCP_FRESH_START.md)
- [infra/gcp/README.md](/c:/code/option_trading/infra/gcp/README.md)
- [ops/gcp/README.md](/c:/code/option_trading/ops/gcp/README.md)

## 10. Fill The Operator Template

Copy the template:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

Edit `ops/gcp/operator.env` with your real values.

This is the main input file for the runnable operator scripts.

This file is also the best source of truth when you later create GitHub Actions variables and secrets.

## 11. Provision Base Infrastructure

From the repo root:

```bash
./ops/gcp/from_scratch_bootstrap.sh
```

That script will:

- write `infra/gcp/terraform.tfvars`
- run Terraform
- build and push runtime images
- publish the runtime config bundle

Terraform creates:

- Artifact Registry repo
- model bucket
- runtime config bucket
- runtime VM
- training VM template
- service accounts
- static runtime IP
- firewall rules

The same logical inputs should later be mirrored into GitHub Actions repo variables and environments.

## 12. Prepare The Baseline Runtime Bootstrap Bundle

Prepare `.env.compose` for the baseline runtime first.

Recommended starting point:

- `STRATEGY_ENGINE=deterministic`
- keep `ML_PURE_RUN_ID` blank
- keep `ML_PURE_MODEL_GROUP` blank

The bootstrap script above will publish the runtime config bundle if `.env.compose` already exists.

## 13. Runtime Bring-Up

The runtime VM startup script will:

1. install Docker and Google Cloud CLI
2. clone the repo
3. pull runtime config bundle from GCS
4. pull published models from GCS
5. authenticate to Artifact Registry
6. run Compose

Your runtime VM should now be the only always-on machine.

## 14. Create A Disposable Training VM

Do not keep a large training VM running all the time.

When you need ML work:

```bash
./ops/gcp/create_training_vm.sh
```

Then let it bootstrap the repo and Python environment.

## 15. Supported Release Flow

On the training VM, use the runnable wrapper:

```bash
cd ~/option_trading
./ops/gcp/run_recovery_release_pipeline.sh
```

That wrapper will:

- ensure the virtualenv exists
- install `ml_pipeline_2`
- run training
- run threshold sweep
- refuse unsafe `HOLD` / fallback / utility-failed candidates
- publish locally
- sync the released model group to GCS
- write `release/ml_pure_runtime.env`
- apply the runtime handoff into `.env.compose`
- republish the runtime config bundle

The underlying guarded release command is:

```bash
python -m ml_pipeline_2.run_recovery_release \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.best_1m_e2e.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

## 16. Switch Runtime To The Released ML Model Manually If Needed

Take the generated handoff file and apply it to `.env.compose`:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

This updates `.env.compose` to:

- `STRATEGY_ENGINE=ml_pure`
- `ML_PURE_RUN_ID=<released_run_id>`
- `ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto`

and clears explicit-path ML mode so runtime uses run-id based resolution.

If you want to immediately refresh the runtime config bundle in GCS too:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
export AUTO_PUBLISH_RUNTIME_CONFIG=1
export RUNTIME_CONFIG_BUCKET_URL=gs://<runtime-config-bucket>/runtime
./ops/gcp/apply_ml_pure_release.sh
```

## 17. How GitHub Actions Should Call This

The recommended GitHub Actions model is:

### `ci`

Run on pull requests:

- tests
- static checks
- config validation

### `images`

Run on merge to `main`:

- authenticate to GCP
- call `ops/gcp/build_runtime_images.sh`

### `infra`

Run on manual dispatch or protected promotion:

- generate `infra/gcp/terraform.tfvars`
- run `terraform plan`
- require approval
- run `terraform apply`

### `train-release`

Run on manual dispatch:

- create the disposable training VM
- SSH into that VM or invoke its startup command path
- run `ops/gcp/run_recovery_release_pipeline.sh`

### `deploy-runtime`

Run after an approved release:

- restart or recreate the runtime VM
- or remotely rerun the runtime bootstrap commands

Do not duplicate business logic inside the workflow YAML if it already exists in `ops/gcp` scripts.

## 18. What You Still Need To Do Per Release

Normal human tasks per release:

1. decide whether to run a new training release
2. watch the run and inspect the resulting quality summary
3. approve switching runtime to the new model
4. monitor the runtime after rollout

That is the correct amount of manual involvement.

## 19. Rollout To Runtime VM

After the runtime config bundle has been updated:

- restart the runtime VM, or
- rerun the bootstrap steps on the runtime VM

Because the runtime VM is disposable, recreating it is a valid and often cleaner deployment path.

## 20. Rollback

If a released ML model is not acceptable:

1. apply the previous known-good `ml_pure_runtime.env`, or
2. switch `.env.compose` back to:
   - `STRATEGY_ENGINE=deterministic`
   - blank `ML_PURE_RUN_ID`
   - blank `ML_PURE_MODEL_GROUP`
3. republish the runtime config bundle
4. restart the runtime VM

This keeps rollback simple and fast.

## 21. Day-2 Rules

Follow these rules going forward:

- never use the runtime VM as a build machine
- never treat a training VM as permanent
- never leave the only copy of a model on one VM
- never switch runtime from ad hoc local files
- always publish models to GCS
- always use Terraform for new machine creation
- always let GitHub Actions orchestrate deploys once the workflows are in place

## 22. Which Document To Follow

Use this document as the main guide.

Use these supporting docs only when you need detail:

- [GCP_FRESH_START.md](/c:/code/option_trading/docs/GCP_FRESH_START.md): shorter tear-down/rebuild checklist
- [GCP_DEPLOYMENT.md](/c:/code/option_trading/docs/GCP_DEPLOYMENT.md): deployment architecture and rationale
- [infra/gcp/README.md](/c:/code/option_trading/infra/gcp/README.md): Terraform scaffold details
- [ops/gcp/README.md](/c:/code/option_trading/ops/gcp/README.md): runnable operator scripts
- [ml_pipeline_2/README.md](/c:/code/option_trading/ml_pipeline_2/README.md): ML release commands and experiment notes

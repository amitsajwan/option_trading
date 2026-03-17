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

Do not go back to host-side `nohup python ...` processes on public ports.

## 2. What Must Be Installed Where

### Operator machine

Install these manually:

- `gcloud`
- `terraform`
- `git`
- optional: Docker for local Compose checks

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

## 3. Cost Decision First

If the current 32-core training VM does not contain anything you still need locally:

1. stop it
2. confirm you do not need any unpublished files under:
   - `ml_pipeline_2/artifacts/research`
   - `ml_pipeline_2/artifacts/research_matrices`
   - `ml_pipeline_2/artifacts/published_models`
3. delete it

If you are unsure, export anything important to GCS first.

## 4. Recommended Starting Sizes

Start small and scale only if needed:

- runtime VM: `e2-standard-4`
- training VM template: `n2-standard-8`

Those recommended example sizes are reflected in [terraform.tfvars.example](/c:/code/option_trading/infra/gcp/terraform.tfvars.example).

## 5. Prerequisites

Before provisioning, make sure you have:

- a GCP project
- `gcloud` authenticated to that project
- `terraform` installed
- repo access for the clone URL you will put into Terraform
- a clean `.env.compose` ready for runtime bootstrap
- a Bash-capable operator shell such as Ubuntu, WSL, or Cloud Shell for the helper scripts

Useful repo entry points:

- [GCP_DEPLOYMENT.md](/c:/code/option_trading/docs/GCP_DEPLOYMENT.md)
- [GCP_FRESH_START.md](/c:/code/option_trading/docs/GCP_FRESH_START.md)
- [infra/gcp/README.md](/c:/code/option_trading/infra/gcp/README.md)
- [ops/gcp/README.md](/c:/code/option_trading/ops/gcp/README.md)

## 6. Fill The Operator Template

Copy the template:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
```

Edit `ops/gcp/operator.env` with your real values.

This is the main input file for the runnable operator scripts.

## 7. Provision Base Infrastructure

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

## 8. Prepare The Baseline Runtime Bootstrap Bundle

Prepare `.env.compose` for the baseline runtime first.

Recommended starting point:

- `STRATEGY_ENGINE=deterministic`
- keep `ML_PURE_RUN_ID` blank
- keep `ML_PURE_MODEL_GROUP` blank

The bootstrap script above will publish the runtime config bundle if `.env.compose` already exists.

## 9. Runtime Bring-Up

The runtime VM startup script will:

1. install Docker and Google Cloud CLI
2. clone the repo
3. pull runtime config bundle from GCS
4. pull published models from GCS
5. authenticate to Artifact Registry
6. run Compose

Your runtime VM should now be the only always-on machine.

## 10. Create A Disposable Training VM

Do not keep a large training VM running all the time.

When you need ML work:

```bash
./ops/gcp/create_training_vm.sh
```

Then let it bootstrap the repo and Python environment.

## 11. Supported Release Flow

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

## 12. Switch Runtime To The Released ML Model Manually If Needed

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

## 13. Rollout To Runtime VM

After the runtime config bundle has been updated:

- restart the runtime VM, or
- rerun the bootstrap steps on the runtime VM

Because the runtime VM is disposable, recreating it is a valid and often cleaner deployment path.

## 14. Rollback

If a released ML model is not acceptable:

1. apply the previous known-good `ml_pure_runtime.env`, or
2. switch `.env.compose` back to:
   - `STRATEGY_ENGINE=deterministic`
   - blank `ML_PURE_RUN_ID`
   - blank `ML_PURE_MODEL_GROUP`
3. republish the runtime config bundle
4. restart the runtime VM

This keeps rollback simple and fast.

## 15. Day-2 Rules

Follow these rules going forward:

- never use the runtime VM as a build machine
- never treat a training VM as permanent
- never leave the only copy of a model on one VM
- never switch runtime from ad hoc local files
- always publish models to GCS
- always use Terraform for new machine creation

## 16. Which Document To Follow

Use this document as the main guide.

Use these supporting docs only when you need detail:

- [GCP_FRESH_START.md](/c:/code/option_trading/docs/GCP_FRESH_START.md): shorter tear-down/rebuild checklist
- [GCP_DEPLOYMENT.md](/c:/code/option_trading/docs/GCP_DEPLOYMENT.md): deployment architecture and rationale
- [infra/gcp/README.md](/c:/code/option_trading/infra/gcp/README.md): Terraform scaffold details
- [ops/gcp/README.md](/c:/code/option_trading/ops/gcp/README.md): runnable operator scripts
- [ml_pipeline_2/README.md](/c:/code/option_trading/ml_pipeline_2/README.md): ML release commands and experiment notes

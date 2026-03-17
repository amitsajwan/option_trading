# GCP Fresh Start

This is the clean-from-scratch path for the current repo shape:

- retire the oversized old training VM
- stand up a smaller always-on runtime VM
- create disposable training VMs only when needed
- store app images in Artifact Registry
- store published models in Cloud Storage
- use Terraform so new VMs are reproducible

If you want the full operator-facing step-by-step guide, start with [FROM_SCRATCH_OPERATOR_GUIDE.md](/c:/code/option_trading/docs/FROM_SCRATCH_OPERATOR_GUIDE.md).
If you want the runnable helper scripts, use [ops/gcp/README.md](/c:/code/option_trading/ops/gcp/README.md).

## 1. Retire The Old VM

If you do not need anything local from the current 32-core training VM:

1. stop the VM first
2. verify you do not need any unpublished artifacts under:
   - `ml_pipeline_2/artifacts/research`
   - `ml_pipeline_2/artifacts/research_matrices`
   - `ml_pipeline_2/artifacts/published_models`
3. delete the VM after that check

If there is anything worth keeping, sync it to GCS before deletion.

## 2. Recommended New Sizes

Use smaller sizes first and only scale back up if you hit memory pressure:

- runtime VM: `e2-standard-4`
- training VM template: `n2-standard-8`

That recommendation is now reflected in [terraform.tfvars.example](/c:/code/option_trading/infra/gcp/terraform.tfvars.example).

## 3. Provision Base Infra

From the repo root:

```bash
cd infra/gcp
cp terraform.tfvars.example terraform.tfvars
```

Fill in at least:

- `project_id`
- `region`
- `zone`
- `repo_clone_url`
- `repo_ref`
- `model_bucket_name`
- `runtime_config_bucket_name`
- `runtime_config_sync_source`
- `published_models_sync_source`
- `data_sync_source`

Then apply:

```bash
terraform init
terraform plan
terraform apply
```

Terraform creates:

- Artifact Registry repo
- model bucket
- runtime config bucket
- runtime VM
- training VM template
- service accounts
- firewall rules

## 4. Build Runtime Images

Build from your laptop or any machine with `gcloud`:

```bash
export PROJECT_ID=<gcp-project>
export REGION=asia-south1
export REPOSITORY=option-trading-runtime
export TAG=20260317-1

./ops/gcp/build_runtime_images.sh
```

## 5. Publish Runtime Bootstrap Bundle

Prepare `.env.compose` for the baseline live runtime, then publish it:

```bash
export RUNTIME_CONFIG_BUCKET_URL=gs://<runtime-config-bucket>/runtime
./ops/gcp/publish_runtime_config.sh
```

At this stage you can keep:

- `STRATEGY_ENGINE=deterministic`
- blank `ML_PURE_*` values

## 6. Let Runtime VM Boot Cleanly

The runtime VM startup script will:

1. clone the repo
2. sync `.env.compose` and optional credentials from GCS
3. sync published models from GCS
4. authenticate to Artifact Registry
5. run Compose

This should replace all old `nohup python ...` style process management.

## 7. Create A Training VM Only When Needed

When you actually need training, create a VM from the Terraform output instance template instead of keeping a big machine always on.

That VM should be treated as disposable.

## 8. Run The Guarded Release Flow

On the training VM:

```bash
cd ~/option_trading
source .venv/bin/activate
python -m pip install -e ./ml_pipeline_2

python -m ml_pipeline_2.run_recovery_release \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.best_1m_e2e.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

That will:

- run training
- run threshold sweep
- block unsafe candidates
- publish locally
- sync the published model group to GCS
- write `release/ml_pure_runtime.env`

## 9. Apply The Release To Runtime Config

Still from a repo checkout:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

If you also want to refresh the runtime config bundle in GCS immediately:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
export AUTO_PUBLISH_RUNTIME_CONFIG=1
export RUNTIME_CONFIG_BUCKET_URL=gs://<runtime-config-bucket>/runtime
./ops/gcp/apply_ml_pure_release.sh
```

This updates `.env.compose` to:

- `STRATEGY_ENGINE=ml_pure`
- `ML_PURE_RUN_ID=<released_run_id>`
- `ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto`

and clears explicit-path mode.

## 10. Restart Runtime

After publishing the updated runtime config bundle:

- restart the runtime VM, or
- rerun the runtime bootstrap steps on the runtime VM

Because the runtime VM is disposable, rebuilding it is acceptable and often cleaner than patching a long-lived machine.

## 11. Operating Rule Going Forward

Use this split consistently:

- code: Artifact Registry
- models: Cloud Storage
- infra: Terraform
- runtime: small always-on VM
- training: disposable VM only when needed

That is the path that prevents VM-by-VM drift and keeps cost under control.

## 12. Stop Vs Destroy

Use the right action for the goal:

- `stop runtime VM`
  - lowest-friction pause for compute cost
- `delete training VM`
  - normal cleanup after training
- `destroy infra but preserve data`
  - remove most cost-bearing infra while keeping Artifact Registry images and GCS model/config data
- `terraform destroy`
  - full teardown of managed infra

Typical commands:

```bash
./ops/gcp/stop_runtime.sh
./ops/gcp/delete_training_vm.sh
./ops/gcp/destroy_infra_preserve_data.sh
cd infra/gcp && terraform destroy
```

Note:

- `destroy_infra_preserve_data.sh` is the recommended teardown if you want to keep images and published models
- bucket deletion may require emptying the buckets first
- ad hoc VMs created outside Terraform state must be deleted separately

## 13. Recommended End-Of-Day Flow

If you are done for the day and do not want heavy resources running, use one of these paths.

### Option A: cheapest quick pause

Use this when you want to keep all infrastructure and resume quickly:

```bash
./ops/gcp/delete_training_vm.sh
./ops/gcp/stop_runtime.sh
```

This keeps:

- runtime VM definition
- static IP
- firewall rules
- service accounts
- training instance template
- Artifact Registry images
- model bucket
- runtime config bucket

### Option B: deeper teardown but preserve deployable state

Use this when you want to remove most cost-bearing infra but keep images and published models:

```bash
AUTO_APPROVE=1 ./ops/gcp/destroy_infra_preserve_data.sh
```

This keeps:

- Artifact Registry images
- published models in GCS
- runtime config bundle in GCS

This removes:

- runtime VM
- training instance template
- firewall rules
- static IP
- runtime/training service accounts

## 14. How To Recreate Later

If you used the preserve-data teardown, recreate compute later with:

```bash
export PATH="$HOME/bin:$PATH"
RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Use this because:

- images are already in Artifact Registry
- runtime config is already in GCS
- models are already in GCS

So you only need to recreate the infra and let the runtime VM boot.

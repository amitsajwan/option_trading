# ml_pipeline_2 GCP and User Guide

This is the canonical operator guide for running `ml_pipeline_2`.

It covers:
- local Ubuntu execution
- disposable GCP training-VM execution
- staged training, publish, and runtime handoff

It does not cover:
- raw historical snapshot rebuild from archive
- first-time GCP bootstrap
- runtime VM deployment or cutover

For those external lanes, use:
- `docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- `docs/GCP_BOOTSTRAP_RUNBOOK.md`
- `docs/GCP_DEPLOYMENT.md`

## Supported Operator Path

The supported path for this branch is the staged 1 / 2 / 3 release lane:

1. make sure staged parquet inputs already exist
2. sync them locally if needed
3. validate `configs/research/staged_dual_recipe.default.json`
4. run `ml_pipeline_2.run_staged_release`
5. inspect `summary.json` and publish outputs
6. apply `release/ml_pure_runtime.env`
7. continue with runtime deployment outside this package

## Required Inputs

Local cache root:
- `.data/ml_pipeline`

Required datasets:
- `.data/ml_pipeline/parquet_data/snapshots/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/snapshots_ml_flat/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage1_entry_view/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage2_direction_view/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage3_recipe_view/year=YYYY/data.parquet`

Supported manifest:
- `ml_pipeline_2/configs/research/staged_dual_recipe.default.json`

## Lane A: Local Ubuntu or Existing GCP VM

Use this lane when you already have a machine and only need to run the training package itself.

### 1. Clone and Install

```bash
git clone <repo-url>
cd option_trading
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./ml_pipeline_2
```

### 2. Sync Inputs

```bash
mkdir -p .data
gsutil -m rsync -r gs://option-trading-ml/data .data/ml_pipeline
```

Direct `gs://` manifest paths are intentionally unsupported. Sync locally first.

### 3. Validate the Manifest

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --validate-only
```

Optional resolved-config print:

```bash
ml-pipeline-research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --print-resolved-config
```

### 4. Run Research Only

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json
```

This writes a run directory under:

- `ml_pipeline_2/artifacts/research/staged_dual_recipe_<timestamp>/`

### 5. Run the Supported Staged Release Flow

```bash
python -m ml_pipeline_2.run_staged_release \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

This flow:
1. validates the manifest
2. trains Stage 1, Stage 2, and Stage 3
3. selects policy on `research_valid`
4. scores `final_holdout` once
5. applies hard gates
6. publishes the staged runtime bundle
7. writes `release/ml_pure_runtime.env`

### 6. Publish an Existing Completed Run

```bash
python -m ml_pipeline_2.run_publish_model \
  --run-dir ml_pipeline_2/artifacts/research/<run_id> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

Use this when training already finished and you only need publish.

## Lane B: Disposable GCP Training VM

Use this lane when the repo's `ops/gcp` helpers manage the machine lifecycle for you.

### Preconditions

- base GCP bootstrap is already complete
- parquet data already exists
- `ops/gcp/operator.env` is current
- the intended repo ref is available

Values to verify in `ops/gcp/operator.env`:
- `PROJECT_ID`
- `ZONE`
- `TRAINING_VM_NAME`
- `MODEL_GROUP`
- `PROFILE_ID`
- `STAGED_CONFIG`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`

### 1. Create the VM

```bash
./ops/gcp/create_training_vm.sh
```

### 2. Connect

```bash
gcloud compute ssh "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

On the VM:

```bash
cd /opt/option_trading
git fetch --all --tags
git checkout "${REPO_REF}"
git pull --ff-only
```

### 3. Run the Training Pipeline Script

```bash
./ops/gcp/run_staged_release_pipeline.sh
```

That script is the managed wrapper around the staged release lane. It installs the package, runs `ml_pipeline_2.run_staged_release`, applies the generated ML runtime env, and republishes runtime config.

### 4. Inspect Results

Model bucket:

```bash
gcloud storage ls "${MODEL_BUCKET_URL}"
```

Runtime config bucket:

```bash
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

Latest local env handoff on the VM:

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/ml_pure_runtime.env" | sort | tail -n 1
```

### 5. Delete the VM When Finished

```bash
./ops/gcp/delete_training_vm.sh
```

## What a Successful Staged Release Produces

Within the run directory:
- `summary.json`
- `resolved_config.json`
- `stages/stage1/model.joblib`
- `stages/stage2/model.joblib`
- `stages/stage3/recipes/<recipe_id>/model.joblib`
- stage training reports
- `release/ml_pure_runtime.env`
- `release/release_summary.json`

Within the published model group:
- `model/model.joblib`
- `config/profiles/<profile_id>/threshold_report.json`
- `config/profiles/<profile_id>/training_report.json`

## Runtime Handoff

The staged release writes:

- `STRATEGY_ENGINE=ml_pure`
- `ML_PURE_RUN_ID=<published_run_id>`
- `ML_PURE_MODEL_GROUP=<model_group>`

Apply it with:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_id>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

Live runtime deployment still happens outside this package.

## Failure Signals

Stop and investigate if:
- manifest validation fails
- staged release returns `HOLD`
- `summary.json` is missing `publish_assessment`
- the model bucket does not receive published artifacts
- the runtime handoff file is missing or incomplete

## Documents That Redirect Here

The following older locations now point to this guide:
- `ml_pipeline_2/docs/ubuntu_gcp_runbook.md`
- `docs/TRAINING_RELEASE_RUNBOOK.md`

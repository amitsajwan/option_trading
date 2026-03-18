# ml_pipeline_2

`ml_pipeline_2` is the supported staged ML package for this repo.

The active operator path is:

1. build snapshot parquet and stage views
2. run staged training and release
3. publish a staged runtime bundle
4. switch live runtime with `ML_PURE_RUN_ID` and `ML_PURE_MODEL_GROUP`

Legacy recovery-only research code may still exist in-tree for archived analysis, but it is not the supported release lane for this branch.

## Install

Run from repo root on Ubuntu or GCP:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./ml_pipeline_2
```

Installed console scripts:

- `ml-pipeline-research`
- `ml-pipeline-staged-release`
- `ml-pipeline-publish-model`

## Supported Data Contract

Sync inputs from GCS into the local ignored cache `.data/ml_pipeline`.

Required staged parquet datasets:

- `.data/ml_pipeline/parquet_data/snapshots/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/snapshots_ml_flat/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage1_entry_view/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage2_direction_view/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage3_recipe_view/year=YYYY/data.parquet`

Sync example:

```bash
mkdir -p .data
gsutil -m rsync -r gs://option-trading-ml/data .data/ml_pipeline
```

Direct `gs://` manifest paths are intentionally unsupported. Sync locally first, then run.

## Checked-In Manifest

The supported staged manifest is:

- [configs/research/staged_dual_recipe.default.json](configs/research/staged_dual_recipe.default.json)

It is explicit by design:

- view IDs
- labeler IDs
- trainer IDs
- policy IDs
- runtime gate IDs
- recipe catalog ID
- windows
- CV config
- hard gates

No implicit training defaults are part of the staged operator flow.

## Common Commands

Validate the staged manifest:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --validate-only
```

Print the resolved config:

```bash
ml-pipeline-research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --print-resolved-config
```

Run a staged research job:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json
```

Run the supported end-to-end staged release flow:

```bash
python -m ml_pipeline_2.run_staged_release \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

Publish an already-completed staged run:

```bash
python -m ml_pipeline_2.run_publish_model \
  --run-dir ml_pipeline_2/artifacts/research/<run_name>_<timestamp> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

`run_publish_model` auto-detects staged vs recovery runs. Staged publish does not support unsafe override.

## What The Staged Release Produces

Each staged run writes:

- `summary.json`
- `stages/stage1/model.joblib`
- `stages/stage2/model.joblib`
- `stages/stage3/recipes/<recipe_id>/model.joblib`
- `stages/*/training_report.json`
- `stages/*/feature_contract.json`

Each successful staged publish writes:

- published staged runtime bundle at `artifacts/published_models/<model_group>/model/model.joblib`
- staged runtime policy at `config/profiles/<profile_id>/threshold_report.json`
- run summary at `config/profiles/<profile_id>/training_report.json`
- `release/ml_pure_runtime.env`

The runtime bundle contains:

- Stage 1 model package
- Stage 2 model package
- Stage 3 recipe packages
- recipe catalog
- runtime prefilter gate order

The runtime policy contains:

- Stage 1 threshold
- Stage 2 CE/PE thresholds and min edge
- Stage 3 recipe threshold and recipe margin

## Live Runtime Handoff

The staged release writes `release/ml_pure_runtime.env` with:

- `STRATEGY_ENGINE=ml_pure`
- `ML_PURE_RUN_ID=<published_run_id>`
- `ML_PURE_MODEL_GROUP=<model_group>`

Apply it into `.env.compose` with:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

Live runtime resolves the staged bundle by run ID and model group. No explicit model path is required in the normal operator path.

## Operator Docs

- From-scratch operator index: [../docs/FROM_SCRATCH_OPERATOR_GUIDE.md](../docs/FROM_SCRATCH_OPERATOR_GUIDE.md)
- Training release runbook: [../docs/TRAINING_RELEASE_RUNBOOK.md](../docs/TRAINING_RELEASE_RUNBOOK.md)
- Ubuntu/GCP ML runbook: [docs/ubuntu_gcp_runbook.md](docs/ubuntu_gcp_runbook.md)
- Runtime deploy/cutover: [../docs/GCP_DEPLOYMENT.md](../docs/GCP_DEPLOYMENT.md)

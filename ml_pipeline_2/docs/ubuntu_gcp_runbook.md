# Ubuntu/GCP Staged ML Runbook

This is the supported Ubuntu VM flow for staged `ml_pipeline_2` training and publish.

## Runtime Model

Use two machines with different responsibilities:

- Windows laptop: write code, inspect artifacts, manage Git
- Ubuntu GCP VM: run staged training, evaluation, and publish

No Windows runtime is required for ML execution.

## External Data Contract

The source of truth is GCS, not Git.

Required staged parquet datasets:

- `gs://option-trading-ml/data/parquet_data/snapshots/year=YYYY/data.parquet`
- `gs://option-trading-ml/data/parquet_data/snapshots_ml_flat/year=YYYY/data.parquet`
- `gs://option-trading-ml/data/parquet_data/stage1_entry_view/year=YYYY/data.parquet`
- `gs://option-trading-ml/data/parquet_data/stage2_direction_view/year=YYYY/data.parquet`
- `gs://option-trading-ml/data/parquet_data/stage3_recipe_view/year=YYYY/data.parquet`

Supported local cache root:

- `.data/ml_pipeline`

Expected synced layout:

```text
.data/ml_pipeline/
|-- parquet_data/
|   |-- snapshots/
|   |-- snapshots_ml_flat/
|   |-- stage1_entry_view/
|   |-- stage2_direction_view/
|   `-- stage3_recipe_view/
`-- published_models/  # optional local publish sync target
```

## Repo Setup

```bash
git clone <repo-url>
cd option_trading
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./ml_pipeline_2
```

## Sync Inputs From GCS

```bash
mkdir -p .data
gsutil -m rsync -r gs://option-trading-ml/data .data/ml_pipeline
```

Direct `gs://` manifest paths are intentionally unsupported. Sync locally first, then run.

## Validate The Checked-In Staged Manifest

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --validate-only
```

Inspect resolved paths:

```bash
ml-pipeline-research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --print-resolved-config
```

## Run A Staged Research Job

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json
```

This produces a staged run under:

- `ml_pipeline_2/artifacts/research/staged_dual_recipe_<timestamp>/`

Key outputs:

- `summary.json`
- `stages/stage1/model.joblib`
- `stages/stage2/model.joblib`
- `stages/stage3/recipes/<recipe_id>/model.joblib`
- feature contracts and training reports per stage

## Run The Supported Release Flow

The supported publish lane is:

```bash
python -m ml_pipeline_2.run_staged_release \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

This flow:

1. trains Stage 1, 2, and 3
2. selects policy on `research_valid`
3. scores `final_holdout` once
4. blocks publish on any hard-gate failure
5. publishes the staged runtime bundle
6. writes `release/ml_pure_runtime.env`

## Publish An Existing Completed Staged Run

```bash
python -m ml_pipeline_2.run_publish_model \
  --run-dir ml_pipeline_2/artifacts/research/<run_name>_<timestamp> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

`run_publish_model` auto-detects staged vs recovery runs. Staged publish does not allow unsafe override.

## Live Handoff

Use the generated env file from the release:

```bash
cat ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
```

It should contain:

- `STRATEGY_ENGINE=ml_pure`
- `ML_PURE_RUN_ID=<published_run_id>`
- `ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto`

Apply it to `.env.compose`:

```bash
export RELEASE_ENV_PATH=ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/release/ml_pure_runtime.env
./ops/gcp/apply_ml_pure_release.sh
```

## Notes On Legacy Paths

Older recovery manifests and recovery-only release scripts remain in the repo for historical/reference purposes, but they are not the supported operator path for this branch. The supported release path is the staged 1/2/3 flow above.

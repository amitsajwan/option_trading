# ml_pipeline_2

`ml_pipeline_2` is the repo's ML training and publish package for the staged `ml_pure` lane.

It owns:
- manifest-driven training configuration
- staged Stage 1 / Stage 2 / Stage 3 training on historical snapshot-derived parquet
- holdout evaluation and publish gating
- published runtime bundle and runtime policy generation

It does not own:
- historical parquet generation from raw market data
- live snapshot production
- live runtime execution
- GCP bootstrap or runtime deployment

The supported operator flow is:
1. build or sync snapshot-derived parquet inputs
2. validate the staged manifest
3. run staged research or `run_staged_release`
4. publish the staged runtime bundle
5. hand off `ML_PURE_RUN_ID` and `ML_PURE_MODEL_GROUP` to `strategy_app`

## Canonical Documentation

Module-local docs now live under `ml_pipeline_2/docs`:

- [Architecture](docs/architecture.md)
- [Detailed Design and Source Inventory](docs/detailed_design.md)
- [GCP and User Guide](docs/gcp_user_guide.md)
- [Supported staged manifest](configs/research/staged_dual_recipe.default.json)

Use the detailed design doc when you need a file-by-file map of `src/ml_pipeline_2`.

## Quick Start

Install from repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./ml_pipeline_2
```

Validate the supported staged manifest:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --validate-only
```

Run the supported staged release lane:

```bash
python -m ml_pipeline_2.run_staged_release \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

## Inputs and Outputs

Required staged parquet datasets:

- `.data/ml_pipeline/parquet_data/snapshots/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/snapshots_ml_flat/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage1_entry_view/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage2_direction_view/year=YYYY/data.parquet`
- `.data/ml_pipeline/parquet_data/stage3_recipe_view/year=YYYY/data.parquet`

Primary staged run outputs:

- `ml_pipeline_2/artifacts/research/<run_id>/summary.json`
- `ml_pipeline_2/artifacts/research/<run_id>/stages/stage1/model.joblib`
- `ml_pipeline_2/artifacts/research/<run_id>/stages/stage2/model.joblib`
- `ml_pipeline_2/artifacts/research/<run_id>/stages/stage3/recipes/<recipe_id>/model.joblib`
- `ml_pipeline_2/artifacts/research/<run_id>/release/ml_pure_runtime.env`

Published outputs:

- `ml_pipeline_2/artifacts/published_models/<model_group>/model/model.joblib`
- `ml_pipeline_2/artifacts/published_models/<model_group>/config/profiles/<profile_id>/threshold_report.json`
- `ml_pipeline_2/artifacts/published_models/<model_group>/config/profiles/<profile_id>/training_report.json`

## External Docs That Stay Outside This Module

These documents remain repo-level because they are not owned by `ml_pipeline_2` itself:

- historical parquet build: `docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- GCP bootstrap and infra bring-up: `docs/GCP_BOOTSTRAP_RUNBOOK.md`
- live runtime deployment and cutover: `docs/GCP_DEPLOYMENT.md`
- cross-system operator routing: `docs/FROM_SCRATCH_OPERATOR_GUIDE.md`

The old repo-level training release runbook now redirects into `ml_pipeline_2/docs/gcp_user_guide.md`.

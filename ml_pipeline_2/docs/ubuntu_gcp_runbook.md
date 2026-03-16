# Ubuntu/GCP Runbook

## Runtime Model

Use two machines with different responsibilities:
- Windows laptop: write code, commit, push, inspect artifacts
- Ubuntu GCP VM: run training, recovery matrices, and quick research flows

No Windows runtime is required for ML execution.

## External Data Contract

The source of truth is GCS, not git.

Required objects:
- `gs://option-trading-ml/data/frozen/model_window_features.parquet`
- `gs://option-trading-ml/data/frozen/holdout_features.parquet`
- `gs://option-trading-ml/data/snapshots_ml_flat/year=YYYY/data.parquet`

Supported local cache root:
- `.data/ml_pipeline`

Expected synced layout:

```text
.data/ml_pipeline/
├── frozen/
│   ├── model_window_features.parquet
│   └── holdout_features.parquet
└── snapshots_ml_flat/
    └── year=YYYY/
        └── data.parquet
```

## Repo Setup

Clone the repo on the Ubuntu VM and install the package in editable mode:

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
gsutil ls gs://option-trading-ml/data/frozen/
```

Direct `gs://` paths inside manifests are intentionally unsupported. The package reads only local filesystem paths.

## Example GCS Bootstrap

If you are creating and using your own bucket, this is the concrete sequence for the frozen research inputs:

```powershell
# Step 1 - Create the bucket
gsutil mb -l asia-south1 gs://option-trading-ml-amit

# Step 2 - Upload model window parquet
gsutil cp ".data\ml_pipeline\frozen\model_window_features.parquet" gs://option-trading-ml-amit/data/frozen/model_window_features.parquet

# Step 3 - Upload holdout parquet
gsutil cp ".data\ml_pipeline\frozen\holdout_features.parquet" gs://option-trading-ml-amit/data/frozen/holdout_features.parquet

# Step 4 - Verify both files are there
gsutil ls -lh gs://option-trading-ml-amit/data/frozen/
```

Then on the Ubuntu VM:

```bash
mkdir -p .data/ml_pipeline/frozen
gsutil cp gs://option-trading-ml-amit/data/frozen/model_window_features.parquet .data/ml_pipeline/frozen/
gsutil cp gs://option-trading-ml-amit/data/frozen/holdout_features.parquet .data/ml_pipeline/frozen/
```

The checked-in manifests expect these final local paths on the VM:
- `.data/ml_pipeline/frozen/model_window_features.parquet`
- `.data/ml_pipeline/frozen/holdout_features.parquet`

## Validate The Checked-In Manifests

The checked-in configs under `ml_pipeline_2/configs/research` already point at the Ubuntu cache layout through relative paths.

Validate recovery:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json \
  --validate-only
```

Run the verified 1-month end-to-end recovery smoke:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.best_1m_e2e.json
```

Validate the stronger 1-month tuning base manifest:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.tuning_1m_e2e.json \
  --validate-only
```

Validate the full-window 4-year tuning base manifest:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.tuning_4y.json \
  --validate-only
```

Validate the narrowed 4-year fast-path base manifest:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.fast_path_4y.json \
  --validate-only
```

Validate phase 2:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/phase2_label_sweep.default.json \
  --validate-only
```

Inspect resolved paths:

```bash
ml-pipeline-research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json \
  --print-resolved-config
```

## Launch Recovery Matrix

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.default.json
```

The matrix config uses:
- base manifest: `fo_expiry_aware_recovery.default.json`
- matrix root: `ml_pipeline_2/artifacts/research_matrices`
- background job root: `ml_pipeline_2/artifacts/background_jobs`

## Stronger Tuning Workflow

Run the short tuning sweep first:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.tuning_1m_e2e.json
```

This matrix uses:
- base manifest: `fo_expiry_aware_recovery.tuning_1m_e2e.json`
- 1 recipe: `FIXED_H15_TP30_SL12`
- 1 feature set: `fo_expiry_aware_v2`
- tuned tree models:
  - `xgb_shallow`
  - `xgb_balanced`
  - `xgb_regularized`
  - `xgb_deep_v1`
  - `xgb_deep_slow_v1`
  - `lgbm_fast`
  - `lgbm_dart`
  - `lgbm_large_v1`
  - `lgbm_large_dart_v1`
- background launch cap: `3`

Refill the next batch when one or more jobs complete:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --launch-pending \
  --matrix-root ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp> \
  --max-parallel 3
```

After the 1-month sweep is reviewed, run the 5-month tuning matrix:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.tuning_5m.json
```

This keeps the same tuned tree model list, keeps `max_parallel=3`, and expands back to the recovery recipe grid plus the 3 selected feature sets.

Before any broader 4-year expansion, run the narrowed fast-path matrix:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.fast_path_4y.json
```

This run uses:
- `full_model`: `2020-08-03` to `2024-07-31`
- `final_holdout`: `2024-08-01` to `2024-10-31`
- `2 feature sets x 2 models = 4 combos`
- the exact 4 narrowed `H15 / TP30 / SL8-10` recipes
- `event_sampling_mode=cusum`
- candidate filtering on event-sampled, non-expiry-day, non-high-ATR, tradeable-context rows
- `max_parallel=4`
- `meta_gate.enabled=false`
- `resume_primary=true`

This fast path is the preferred deployable lane after the wider exploratory runs. Do not restart the full `recovery_matrix.tuning_4y.json` unless the fast path still fails to produce a publishable candidate.

Keep the fast-path matrix topped up:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --watch-pending \
  --matrix-root ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp> \
  --max-parallel 4 \
  --retry-failed \
  --poll-seconds 120
```

If you only want a one-shot refill instead of the watcher:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --launch-pending \
  --matrix-root ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp> \
  --max-parallel 4 \
  --retry-failed
```

After a combo completes, sweep the narrowed threshold grid:

```bash
python -m ml_pipeline_2.run_recovery_threshold_sweep \
  --run-dir ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp>/runs/<combo_key>/<run_dir> \
  --threshold-grid 0.30 0.35 0.40 0.45 0.50
```

Then inspect:
- `artifacts/research_matrices/<matrix_name_timestamp>/report.json`
- `artifacts/research_matrices/<matrix_name_timestamp>/report.csv`
- `artifacts/research_matrices/<matrix_name_timestamp>/recipe_report.csv`

The matrix report now includes:
- `recipes_completed`
- `recipes_total`
- `last_state_event`
- `last_event_ts`
- `current_recipe_id`

If you need to rerun a single combo directly into its existing run directory, reuse the output root:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.fast_path_4y.json \
  --run-output-root ml_pipeline_2/artifacts/research/<run_name>_<timestamp>
```

When the chosen combo has a good sweep result, publish it with the sweep-selected threshold:

```bash
python -m ml_pipeline_2.run_publish_model \
  --run-dir ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp>/runs/<combo_key>/<run_dir> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --threshold-source threshold_sweep_recommended
```

Then switch `strategy_app` to the published run through the existing Pure ML model-switch flow. No runtime architecture change is required for this path.

## Quick Research Flows

Stage 1 move detector:

```bash
python -m ml_pipeline_2.run_move_detector_quick \
  --config ml_pipeline_2/configs/research/move_detector_quick.default.json
```

Stage 2 direction from a completed Stage 1 run:

```bash
python -m ml_pipeline_2.run_direction_from_move_quick \
  --config ml_pipeline_2/configs/research/direction_from_move_quick.default.json
```

The direction config still needs a concrete `inputs.stage1_run_dir`. That path should point to a completed Stage 1 run under `ml_pipeline_2/artifacts/research/...`.

## Outputs

Research runs write under:
- `ml_pipeline_2/artifacts/research/<run_name>_<timestamp>/`

Recovery matrices write under:
- `ml_pipeline_2/artifacts/research_matrices/<matrix_name>_<timestamp>/`

Background job metadata writes under:
- `ml_pipeline_2/artifacts/background_jobs/<job_id>/`

## Operational Rules

- Do not commit parquet inputs, outputs, caches, or temp files.
- Keep `.data/ml_pipeline` local and ignored.
- Use checked-in manifests for the supported Ubuntu path layout.
- If the GCS objects move, update the runbook and manifests together.

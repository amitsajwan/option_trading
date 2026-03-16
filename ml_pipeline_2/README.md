# ml_pipeline_2

`ml_pipeline_2` is the research-only ML package for manifest-driven experiments on frozen feature inputs.

Supported runtime model:
- Windows laptop: code, git, result inspection
- Ubuntu GCP VM: all training, matrix runs, and artifact generation

The supported data contract is external to git. Sync inputs from GCS into a local ignored cache before running:
- `gs://option-trading-ml/data/frozen/model_window_features.parquet`
- `gs://option-trading-ml/data/frozen/holdout_features.parquet`
- `gs://option-trading-ml/data/snapshots_ml_flat/year=YYYY/data.parquet`

The supported local cache root is `.data/ml_pipeline`, so the checked-in manifests resolve to:
- `.data/ml_pipeline/frozen/model_window_features.parquet`
- `.data/ml_pipeline/frozen/holdout_features.parquet`
- `.data/ml_pipeline/snapshots_ml_flat/year=YYYY/data.parquet`

## Install

Run from the repo root on Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./ml_pipeline_2
```

Installed console scripts:
- `ml-pipeline-research`
- `ml-pipeline-move-detector`
- `ml-pipeline-direction`
- `ml-pipeline-recovery-matrix`
- `ml-pipeline-background-job`
- `ml-pipeline-publish-model`

## Sync Inputs

```bash
mkdir -p .data
gsutil -m rsync -r gs://option-trading-ml/data .data/ml_pipeline
gsutil ls gs://option-trading-ml/data/frozen/
```

Direct `gs://` manifest inputs are not supported in this branch. Sync locally first, then run against local files.

## Checked-In Manifests

The checked-in research configs are Ubuntu-ready and resolve paths relative to their config directory:
- [`configs/research/phase2_label_sweep.default.json`](configs/research/phase2_label_sweep.default.json)
- [`configs/research/fo_expiry_aware_recovery.default.json`](configs/research/fo_expiry_aware_recovery.default.json)
- [`configs/research/fo_expiry_aware_recovery.best_1m_e2e.json`](configs/research/fo_expiry_aware_recovery.best_1m_e2e.json)
- [`configs/research/fo_expiry_aware_recovery.tuning_1m_e2e.json`](configs/research/fo_expiry_aware_recovery.tuning_1m_e2e.json)
- [`configs/research/fo_expiry_aware_recovery.tuning_5m.json`](configs/research/fo_expiry_aware_recovery.tuning_5m.json)
- [`configs/research/fo_expiry_aware_recovery.tuning_4y.json`](configs/research/fo_expiry_aware_recovery.tuning_4y.json)
- [`configs/research/fo_expiry_aware_recovery.fast_path_4y.json`](configs/research/fo_expiry_aware_recovery.fast_path_4y.json)
- [`configs/research/move_detector_quick.default.json`](configs/research/move_detector_quick.default.json)
- [`configs/research/direction_from_move_quick.default.json`](configs/research/direction_from_move_quick.default.json)
- [`configs/research/recovery_matrix.default.json`](configs/research/recovery_matrix.default.json)
- [`configs/research/recovery_matrix.tuning_1m_e2e.json`](configs/research/recovery_matrix.tuning_1m_e2e.json)
- [`configs/research/recovery_matrix.tuning_5m.json`](configs/research/recovery_matrix.tuning_5m.json)
- [`configs/research/recovery_matrix.tuning_4y.json`](configs/research/recovery_matrix.tuning_4y.json)
- [`configs/research/recovery_matrix.fast_path_4y.json`](configs/research/recovery_matrix.fast_path_4y.json)

Default output roots resolve into `ml_pipeline_2/artifacts/...`.

## Common Commands

Validate a research manifest:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json \
  --validate-only
```

Print the resolved research config:

```bash
ml-pipeline-research \
  --config ml_pipeline_2/configs/research/phase2_label_sweep.default.json \
  --print-resolved-config
```

Run the recovery matrix:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.default.json
```

Run the stronger 1-month tuning sweep:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.tuning_1m_e2e.json
```

Refill a capped matrix after one or more background jobs finish:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --launch-pending \
  --matrix-root ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp> \
  --max-parallel 3
```

Keep a matrix topped up automatically until it finishes:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --watch-pending \
  --matrix-root ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp> \
  --max-parallel 3 \
  --poll-seconds 120
```

Run the verified 1-month end-to-end recovery smoke:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.best_1m_e2e.json
```

Publish a completed recovery run for runtime consumption:

```bash
python -m ml_pipeline_2.run_publish_model \
  --run-dir ml_pipeline_2/artifacts/research/<run_name>_<timestamp> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

Run the Stage 1 move detector:

```bash
python -m ml_pipeline_2.run_move_detector_quick \
  --config ml_pipeline_2/configs/research/move_detector_quick.default.json
```

Run Stage 2 direction from a completed Stage 1 run:

```bash
python -m ml_pipeline_2.run_direction_from_move_quick \
  --config ml_pipeline_2/configs/research/direction_from_move_quick.default.json
```

## Stronger Model Tuning V1

The shared model catalog now includes a first preset-based tuning wave for tree models:
- XGBoost: `xgb_shallow`, `xgb_balanced`, `xgb_regularized`, `xgb_deep_v1`, `xgb_deep_slow_v1`
- LightGBM: `lgbm_fast`, `lgbm_dart`, `lgbm_large_v1`, `lgbm_large_dart_v1`

Resource strategy:
- set per-model tree threads with `training.runtime.model_n_jobs`
- keep same-VM 4-year runs at `max_parallel=1`
- use outer matrix parallelism only when the machine has enough RAM for multiple combos

Recommended staged workflow:
1. Run [`configs/research/recovery_matrix.tuning_1m_e2e.json`](configs/research/recovery_matrix.tuning_1m_e2e.json) first.
2. Review the best completed 1-month combo under `artifacts/research_matrices/.../report.json`.
3. Run [`configs/research/recovery_matrix.tuning_5m.json`](configs/research/recovery_matrix.tuning_5m.json) second.
4. Run the narrowed deployable lane [`configs/research/recovery_matrix.fast_path_4y.json`](configs/research/recovery_matrix.fast_path_4y.json) before any broader 4-year re-expansion.
5. Only widen again if the fast path fails to produce a publishable candidate after threshold sweep.

Fast-path 4-year commands:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --config ml_pipeline_2/configs/research/recovery_matrix.fast_path_4y.json
```

Keep the fast-path matrix topped up:

```bash
python -m ml_pipeline_2.run_recovery_matrix \
  --watch-pending \
  --matrix-root ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp> \
  --max-parallel 4 \
  --retry-failed \
  --poll-seconds 120
```

Run a threshold sweep on a completed combo:

```bash
python -m ml_pipeline_2.run_recovery_threshold_sweep \
  --run-dir ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp>/runs/<combo_key>/<run_dir> \
  --threshold-grid 0.30 0.35 0.40 0.45 0.50
```

Publish the selected run using the sweep-recommended threshold:

```bash
python -m ml_pipeline_2.run_publish_model \
  --run-dir ml_pipeline_2/artifacts/research_matrices/<matrix_name_timestamp>/runs/<combo_key>/<run_dir> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --threshold-source threshold_sweep_recommended
```

The default manifests remain stable. The tuning configs are opt-in.

## Published Models

Published runtime artifacts now live under:

```text
ml_pipeline_2/artifacts/published_models/<model_group>/
├── model/model.joblib
├── config/profiles/<profile_id>/threshold_report.json
├── config/profiles/<profile_id>/training_report.json
├── model_contract.json
└── reports/training/
    ├── run_<run_id>.json
    └── latest.json
```

Per-run copies are also preserved under `data/training_runs/<run_id>/...` so run-id based switching resolves stable historical artifacts instead of whichever model was published most recently.

## Docs

- Ubuntu operator flow: [`docs/ubuntu_gcp_runbook.md`](docs/ubuntu_gcp_runbook.md)
- Bounded-context architecture: [`architecture.md`](architecture.md)

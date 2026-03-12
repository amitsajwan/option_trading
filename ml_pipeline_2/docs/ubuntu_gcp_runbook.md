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

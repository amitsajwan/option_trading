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
- [`configs/research/move_detector_quick.default.json`](configs/research/move_detector_quick.default.json)
- [`configs/research/direction_from_move_quick.default.json`](configs/research/direction_from_move_quick.default.json)
- [`configs/research/recovery_matrix.default.json`](configs/research/recovery_matrix.default.json)

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

Run the verified 1-month end-to-end recovery smoke:

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/fo_expiry_aware_recovery.best_1m_e2e.json
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

## Docs

- Ubuntu operator flow: [`docs/ubuntu_gcp_runbook.md`](docs/ubuntu_gcp_runbook.md)
- Bounded-context architecture: [`architecture.md`](architecture.md)

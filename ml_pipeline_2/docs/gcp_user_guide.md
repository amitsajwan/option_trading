# ml_pipeline_2 GCP and User Guide

This is the detailed module guide for `ml_pipeline_2` on local Ubuntu and disposable GCP training VMs.

The primary operator entrypoint for this workflow is `docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`.
Use this file when you need package-specific detail behind that runbook.

It covers:
- local Ubuntu execution
- disposable GCP training-VM execution
- staged training, publish, and runtime handoff

It does not cover:
- raw historical snapshot rebuild from archive
- first-time GCP bootstrap
- runtime VM deployment or cutover

For those external lanes, use:
- `docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- `docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`
- `docs/runbooks/GCP_DEPLOYMENT.md`

## Supported Operator Path

The supported path for this branch is the staged 1 / 2 / 3 release lane:

1. make sure staged parquet inputs already exist
2. sync them locally if needed
3. validate `configs/research/staged_dual_recipe.default.json`
4. run `ml_pipeline_2.run_staged_release`
5. inspect `summary.json` and publish outputs
6. apply `release/ml_pure_runtime.env`
7. continue with runtime deployment outside this package

Retired paths such as open-search rebaseline and the removed legacy `ml_pipeline` package are not part of the supported operator flow.

On the current branch there is no separate "champion selection" operator step for ML releases.
The staged flow writes `summary.json` with `publish_assessment.decision = PUBLISH|HOLD`.
`run_staged_release` always writes `release/assessment.json` and `release/release_summary.json`, and publishes the runtime bundle only when that staged run is publishable.

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

Windows PowerShell equivalent:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .\ml_pipeline_2
```

### 2. Sync Or Build Inputs

If final staged parquet already exists in GCS, sync it into the local cache root:

```bash
mkdir -p .data/ml_pipeline/parquet_data
gcloud storage rsync \
  "gs://<snapshot-data-bucket>/parquet_data" \
  ".data/ml_pipeline/parquet_data" \
  --recursive
```

If local `market_base` already exists but the staged derived datasets are missing, rebuild them locally:

```bash
python -m snapshot_app.historical.snapshot_batch_runner \
  --build-stage derived \
  --validate-ml-flat-contract
```

There is no repo-wide default training bucket. The old `gs://option-trading-ml/data` example is retired.

Direct `gs://` manifest paths are intentionally unsupported. Sync or build the datasets locally first.

### 3. Validate the Manifest

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --validate-only
```

`--validate-only` checks manifest resolution and runtime dependencies, including staged model backend availability.
It does not prove that the available parquet window is large enough to produce walk-forward folds for the chosen manifest.
If you only built a narrow historical slice, use a matching research manifest instead of `staged_dual_recipe.default.json`.

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
2. runs the Stage 2 signal precheck on the labeled `full_model` window
3. trains Stage 1 and applies the Stage 1 CV precheck
4. trains Stage 2 and applies the Stage 2 CV precheck
5. trains Stage 3 and selects policy on `research_valid` only if the earlier prechecks passed
6. scores `final_holdout` once
7. applies hard gates and computes `publish_assessment`
8. writes `release/assessment.json` and `release/release_summary.json`
9. publishes the staged runtime bundle and writes `release/ml_pure_runtime.env` only on `PUBLISH`

Additional checked-in research manifests:

- `ml_pipeline_2/configs/research/staged_dual_recipe.deep_search.json`
  - broader Stage 1 feature-set and model search
  - use this first when the default manifest holds at Stage 1 and you want a deeper search before changing production thresholds
- `ml_pipeline_2/configs/research/staged_dual_recipe.stage1_hpo.json`
  - keeps the staged release flow unchanged
  - adds Stage 1-only random-search HPO over the requested base models
  - use this when you want actual parameter tuning rather than only more fixed presets
- `ml_pipeline_2/configs/research/staged_dual_recipe.stage2_hpo.json`
  - keeps the broader Stage 1 search from `deep_search`
  - adds Stage 2-only random-search HPO over the requested base models
  - use this when `deep_search` already clears Stage 1 and then holds at `stage2_cv`
- `ml_pipeline_2/configs/research/staged_dual_recipe.stage1_diagnostic.json`
  - keeps the default search space
  - relaxes only the Stage 1 hard gates slightly for diagnosis
  - use this only to learn whether the default Stage 1 gates are narrowly too strict

For managed VM wrapper runs, prefer these research-only commands so the runtime is never changed during investigation:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_hpo" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_hpo.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-hpo.log
```

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_deep" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.deep_search.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-deep.log
```

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_stage2_hpo" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_hpo.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-stage2-hpo.log
```

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_diag" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_diagnostic.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-diag.log
```

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
- `DATA_SYNC_SOURCE`
- `MODEL_GROUP`
- `PROFILE_ID`
- `STAGED_CONFIG`
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`

`DATA_SYNC_SOURCE` must materialize local `.data/ml_pipeline/*` on the VM. It is not a built-in shared bucket name.

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
sudo apt-get update
sudo apt-get install -y tmux
cd /opt/option_trading
git fetch --all --tags
git checkout "${REPO_REF}"
git pull --ff-only
```

### 3. Run the Training Pipeline Script

```bash
tmux new -s training
```

Inside the `tmux` session:

```bash
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release.log
```

Run it from `/opt/option_trading`, or export `REPO_ROOT=/opt/option_trading` before invoking it.

That script is the managed wrapper around the staged release lane. It installs the package, runs `ml_pipeline_2.run_staged_release`, applies the generated ML runtime env, and republishes runtime config.

For GCP VM execution, prefer `tmux` for every long training run.
If the SSH session drops while the command is running in a plain foreground shell, the staged release usually stops with that shell.

Useful commands:

```bash
tmux attach -t training
tmux ls
tail -f /opt/option_trading/training-release.log
```

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
- `release/release_summary.json`
- `release/assessment.json`
- `release/ml_pure_runtime.env` on `PUBLISH` only

Within the published model group:
- `model/model.joblib`
- `config/profiles/<profile_id>/threshold_report.json`
- `config/profiles/<profile_id>/training_report.json`

## Champion Terminology

Older repo history used "champion" language for the removed `ml_pipeline` and open-search flows.

That is not the release contract for the supported staged lane.

For staged `ml_pipeline_2`, the decision path is:

1. run the Stage 2 signal precheck and Stage 1 / 2 CV prechecks
2. if those pass, train all three stages and score `final_holdout`
3. compute `publish_assessment`
4. write `release/assessment.json` and `release/release_summary.json`
5. if `publish_assessment.decision=PUBLISH`, write published artifacts and `release/ml_pure_runtime.env`
6. if `publish_assessment.decision=HOLD`, do not publish and do not write `release/ml_pure_runtime.env`

Use `summary.json`, `release/assessment.json`, and `release/release_summary.json` as the current release records, not a champion registry.

`summary.json` can complete in one of four modes:
- `completed`
- `stage2_signal_check_failed`
- `stage1_cv_gate_failed`
- `stage2_cv_gate_failed`

Every `summary.json` includes `completion_mode` and `cv_prechecks`.
Early-HOLD summaries intentionally omit downstream sections that were not computed, such as `holdout_reports`, `policy_reports`, and `gates`.

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
- `summary.json` is missing `publish_assessment`, `completion_mode`, or `cv_prechecks`
- the model bucket does not receive published artifacts
- `release/assessment.json` or `release/release_summary.json` is missing
- `publish_assessment.decision=PUBLISH` but the runtime handoff file is missing or incomplete

## Related Docs

- `ml_pipeline_2/docs/ubuntu_gcp_runbook.md`
- `docs/runbooks/README.md`
- `docs/runbooks/GCP_DEPLOYMENT.md`

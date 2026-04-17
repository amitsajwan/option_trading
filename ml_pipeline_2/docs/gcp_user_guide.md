# ml_pipeline_2 GCP and User Guide

This is the package-level operator guide for `ml_pipeline_2`.

Use it for:

- local or GCP research execution
- staged single-run, grid, campaign, and factory entrypoints
- preflight and failure checks
- publish and handoff expectations

Use repo-level runbooks for VM bootstrap, parquet rebuild, and deployment outside this package.

## What This Package Owns

`ml_pipeline_2` trains and evaluates staged models against local parquet inputs and can publish a runtime bundle for the `ml_pure` strategy lane.

Primary entrypoints:

- `python -m ml_pipeline_2.run_research`
- `python -m ml_pipeline_2.run_staged_release`
- `python -m ml_pipeline_2.run_staged_grid`
- `python -m ml_pipeline_2.run_training_campaign`
- `python -m ml_pipeline_2.run_training_factory`
- `python -m ml_pipeline_2.run_staged_data_preflight`
- `python -m ml_pipeline_2.run_publish_model`

## What This Guide Does Not Cover

- first-time GCP project bootstrap
- raw archive upload
- full historical snapshot rebuild policy
- runtime VM cutover

For those, use:

- `docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- `docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`
- `docs/runbooks/GCP_DEPLOYMENT.md`

## Required Local Inputs

Local cache root:

- `.data/ml_pipeline`

Expected staged parquet root:

- `.data/ml_pipeline/parquet_data`

Typical datasets:

- `snapshots`
- `snapshots_ml_flat`
- `snapshots_ml_flat_v2`
- `stage1_entry_view`
- `stage1_entry_view_v2`
- `stage2_direction_view`
- `stage2_direction_view_v2`
- `stage3_recipe_view`
- `stage3_recipe_view_v2`

Candidate datasets such as `*_v3_candidate` are valid research inputs when the manifest points to the matching view IDs and the staged registries support them.

Direct `gs://` manifest input paths are intentionally unsupported. Sync or build inputs locally first.

## Supported Workflow Shapes

### 1. Single Research Run

Use when you want one resolved staged manifest executed into one run root.

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json
```

This writes under:

- `ml_pipeline_2/artifacts/research/<run_id>/`

### 2. Staged Release

Use when you want training plus publish assessment and optional published bundle output.

```bash
python -m ml_pipeline_2.run_staged_release \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  --model-bucket-url gs://<model-bucket>/published_models
```

### 3. Research Grid

Use when you want one base staged manifest expanded into multiple comparable runs.

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.prod_v1.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

Grid outputs live under:

- `ml_pipeline_2/artifacts/research/<grid_run_id>/`

with:

- `grid_status.json`
- `grid_summary.json`
- `manifests/`
- `runs/`

### 4. Campaign

Use when you want a higher-level lane generator over one or more grid/research templates.

```bash
python -m ml_pipeline_2.run_training_campaign \
  --spec ml_pipeline_2/configs/campaign/velocity_screen_campaign_v1.json
```

Campaign outputs live under:

- `ml_pipeline_2/artifacts/campaign_runs/<campaign_id>/`

with lane-level roots containing:

- `workflow_status.json`
- `lanes/<lane_id>/runner_output/...`

### 5. Factory

Use when you want a multi-spec orchestration layer above campaigns.

```bash
python -m ml_pipeline_2.run_training_factory \
  --spec <factory_spec.json>
```

## Validate Before Running

### Manifest Validation

```bash
python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json \
  --validate-only
```

`--validate-only` checks manifest resolution and runtime dependency availability.
It does not prove that the available date window is large enough to produce walk-forward folds for the chosen CV geometry.

This matters especially for:

- narrow historical slices
- session-filtered Stage 1 or Stage 2 runs
- direction-only or other heavily filtered Stage 2 research lanes

### Data Preflight

Use preflight when you need dataset/view parity, feature coverage, and temporal-validity checks before a real run:

```bash
python -m ml_pipeline_2.run_staged_data_preflight \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json
```

The current preflight checks:

- support/view key parity
- feature-set resolution
- Stage 2 temporal validity for `ctx_am_*` and `vel_*`
- missing-rate enforcement for resolved feature columns

The velocity temporal-validity check is intended for Stage 2-style views, not as a blanket requirement for every staged dataset.

## Current Training Knobs That Matter

The manifest contract now supports several stage-scoped filters and redesign controls that older docs did not mention:

- `training.stage1_session_filter`
- `training.stage2_session_filter`
- `training.stage2_label_filter`
- `training.stage2_target_redesign`

These are validated in `contracts/manifests.py` and applied inside `staged/pipeline.py`.

Practical implication:

- filtered Stage 1 or Stage 2 lanes can be valid research tools
- they also reduce the available day count and can break walk-forward fold construction if CV windows stay too large

If a filtered run fails with `no walk-forward folds produced`, the fix is usually to shrink the CV geometry for that specific manifest rather than widening the operator guide or bypassing validation.

## Current Stage 2 Variants

The codebase currently supports more than one Stage 2 problem shape.

Examples in checked-in configs include:

- ordinary direction classification
- direction-or-no-trade labeling
- target-redesigned and high-conviction Stage 2 lanes
- session-filtered MIDDAY and MIDDAY+LATE_SESSION lanes

Do not assume every Stage 2 run is the same direction-only baseline.
Read the manifest being launched.

## Long-Running Run Observability

Every long run should be read from persisted status artifacts, not from shell assumptions.

Single-run artifacts:

- `run_status.json`
- `state.jsonl`
- `summary.json`

Grid artifacts:

- `grid_status.json`
- `grid_summary.json`

Campaign artifacts:

- `workflow_status.json`
- lane `runner_output/...` status files

Current staged runs also emit setup progress events before stage training begins.
Look for `prep_start` and `prep_done` events in `state.jsonl` for:

- `support_load`
- `oracle_build`
- `stage_prepare`

This matters because a run can spend substantial time in setup before the first `stage_start` event.

## Typical Local / Existing VM Setup

```bash
git clone <repo-url>
cd option_trading
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./ml_pipeline_2
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .\ml_pipeline_2
```

## Disposable GCP VM Notes

Use `tmux` for long runs on a disposable VM.

Typical flow:

1. create or start the VM outside this package
2. sync repo and parquet inputs
3. activate the repo venv
4. run the desired `ml_pipeline_2` CLI inside `tmux`
5. inspect persisted status files, not only stdout

Useful commands:

```bash
tmux new -s training
tmux attach -t training
tmux ls
```

## What a Successful Staged Release Produces

Within the run directory:

- `summary.json`
- `resolved_config.json`
- stage training reports
- staged model packages
- `release/assessment.json`
- `release/release_summary.json`
- `release/ml_pure_runtime.env` on `PUBLISH` only

Within the published model group:

- `model/model.joblib`
- `config/profiles/<profile_id>/threshold_report.json`
- `config/profiles/<profile_id>/training_report.json`

## Current Release Decision Path

Older repo history used "champion" language for removed legacy flows.
That is not the current release contract for the supported staged lane.

Current decision path:

1. resolve and validate the staged manifest
2. run data and stage preparation
3. run the Stage 2 signal precheck
4. train Stage 1 and apply Stage 1 CV gates
5. train Stage 2 and apply Stage 2 CV gates
6. train Stage 3 and score holdout when earlier gates pass
7. compute `publish_assessment`
8. write release artifacts either way
9. publish the runtime bundle only on `PUBLISH`

Common `completion_mode` values include:

- `completed`
- `stage2_signal_check_failed`
- `stage1_cv_gate_failed`
- `stage2_cv_gate_failed`

## Failure Signals

Stop and investigate if:

- manifest validation fails
- preflight fails
- the run emits `prep_start` but never reaches matching `prep_done` for a long interval
- the run root remains at bare `job_start` with no setup progress on newer code
- `summary.json` is missing after the run reports completion
- a filtered run fails with `no walk-forward folds produced`
- publish assessment is `HOLD` when you expected a release

## Related Docs

- [README.md](README.md)
- [architecture.md](architecture.md)
- [detailed_design.md](detailed_design.md)
- [execution_architecture.md](execution_architecture.md)
- `docs/runbooks/README.md`
- `docs/runbooks/GCP_DEPLOYMENT.md`

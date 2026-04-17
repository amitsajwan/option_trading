# ml_pipeline_2 Architecture

## Purpose

`ml_pipeline_2` is the bounded package that trains historical models and publishes staged runtime artifacts for the `ml_pure` strategy lane.

Its job is to turn snapshot-derived parquet datasets into:
- trained staged model packages
- holdout and publish assessment summaries
- a published runtime bundle
- a runtime policy and env handoff for live deployment

## System Boundary

`ml_pipeline_2` sits between historical data preparation and live runtime consumption.

Upstream:
- `snapshot_app` historical builders create the parquet datasets consumed here
- repo-level GCP data runbooks manage raw archive upload, snapshot rebuilds, and parquet sync

Downstream:
- `strategy_app` loads the published staged bundle when `STRATEGY_ENGINE=ml_pure`
- `strategy_app` consumes live snapshot events and applies the published policy

Not owned here:
- raw market-data ingestion
- live snapshot generation
- live runtime deployment
- GCP bootstrap

## Supported Flow

The supported release lane for this branch is the staged manifest-driven path:

1. validate `configs/research/staged_dual_recipe.default.json`
2. resolve the manifest contract
3. load historical parquet views
4. construct staged oracle labels and training frames
5. run the Stage 2 signal precheck on the labeled `full_model` slice
6. train Stage 1 entry, then apply the Stage 1 CV precheck
7. train Stage 2 direction, then apply the Stage 2 CV precheck
8. train Stage 3 recipe-selection and select policy on `research_valid` only if the earlier prechecks passed
9. score `final_holdout` once on the completed staged policy
10. apply hard gates and compute `publish_assessment`
11. if publishable, publish the staged runtime bundle and runtime policy
12. write release assessment artifacts either way, then hand off `ML_PURE_RUN_ID` and `ML_PURE_MODEL_GROUP` only on `PUBLISH`

Primary entrypoints:
- `src/ml_pipeline_2/run_research.py`
- `src/ml_pipeline_2/run_staged_release.py`
- `src/ml_pipeline_2/run_publish_model.py`
- `src/ml_pipeline_2/run_staged_grid.py`
- `src/ml_pipeline_2/run_training_campaign.py`
- `src/ml_pipeline_2/run_training_factory.py`
- `src/ml_pipeline_2/run_staged_data_preflight.py`

## Data Contract

The supported staged flow expects local parquet datasets under `.data/ml_pipeline/parquet_data`:

- `snapshots`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`

The staged manifest binds those datasets through view IDs, labeler IDs, trainer IDs, policy IDs, runtime gate IDs, windows, and hard gates.

Current staged manifests may also include stage-scoped controls such as:

- `training.stage1_session_filter`
- `training.stage2_session_filter`
- `training.stage2_label_filter`
- `training.stage2_target_redesign`

Direct `gs://` manifest input paths are intentionally unsupported. Inputs are synced locally first, then trained.

## Package Ownership

### `contracts`

Owns manifest kinds, typed payloads, and validation rules.

Key files:
- `src/ml_pipeline_2/contracts/types.py`
- `src/ml_pipeline_2/contracts/manifests.py`

### `catalog`

Owns built-in feature profiles, feature sets, model specs, and checked-in default paths.

Key files:
- `src/ml_pipeline_2/catalog/feature_profiles.py`
- `src/ml_pipeline_2/catalog/feature_sets.py`
- `src/ml_pipeline_2/catalog/models.py`
- `src/ml_pipeline_2/catalog/research_defaults.py`

### `dataset_windowing`

Owns parquet loading, trade-date normalization, and window slicing.

Key file:
- `src/ml_pipeline_2/dataset_windowing/frames.py`

### `labeling`

Owns snapshot-frame preparation, regime/dealer-proxy enrichment, event sampling, and label construction.

Key files:
- `src/ml_pipeline_2/labeling/prepare.py`
- `src/ml_pipeline_2/labeling/engine.py`
- `src/ml_pipeline_2/labeling/event_sampling.py`

### `model_search`

Owns feature filtering, walk-forward splits, event purge logic, metrics, and model training/search.

Key files:
- `src/ml_pipeline_2/model_search/features.py`
- `src/ml_pipeline_2/model_search/walk_forward.py`
- `src/ml_pipeline_2/model_search/event_purge.py`
- `src/ml_pipeline_2/model_search/metrics.py`
- `src/ml_pipeline_2/model_search/search.py`

### `inference_contract`

Owns offline scoring against saved model packages and feature-contract checks.

Key file:
- `src/ml_pipeline_2/inference_contract/predict.py`

### `evaluation`

Owns reusable metrics and promotion helpers used by shared evaluation code.

Key files:
- `src/ml_pipeline_2/evaluation/stage_metrics.py`
- `src/ml_pipeline_2/evaluation/direction.py`
- `src/ml_pipeline_2/evaluation/promotion.py`

### `staged`

Owns the supported release lane:
- staged recipe catalog
- staged registries
- staged training orchestration
- runtime bundle contract
- staged publish and release logic

Key files:
- `src/ml_pipeline_2/staged/pipeline.py`
- `src/ml_pipeline_2/staged/publish.py`
- `src/ml_pipeline_2/staged/registries.py`
- `src/ml_pipeline_2/staged/recipes.py`
- `src/ml_pipeline_2/staged/runtime_contract.py`

### `experiment_control`

Owns run-root creation, manifest resolution persistence, state tracking, and detached background-job metadata.

Key files:
- `src/ml_pipeline_2/experiment_control/runner.py`
- `src/ml_pipeline_2/experiment_control/state.py`
- `src/ml_pipeline_2/experiment_control/background.py`

### `publishing`

Owns published-model root resolution, publish report lookup, and optional release-time GCS sync.

Key files:
- `src/ml_pipeline_2/publishing/publish.py`
- `src/ml_pipeline_2/publishing/resolver.py`
- `src/ml_pipeline_2/publishing/release.py`

### `scenario_flows`

Reserved namespace for scenario-specific orchestration modules. The supported staged lane dispatches directly into `staged.pipeline`.

Key file:
- `src/ml_pipeline_2/scenario_flows/__init__.py`

## Runtime Artifact Model

A successful staged release produces two artifact layers.

Research-run artifacts under `ml_pipeline_2/artifacts/research/<run_id>`:
- resolved manifest
- `run_status.json`
- `state.jsonl`
- staged model packages
- training reports
- `summary.json`
- `release/assessment.json`
- `release/release_summary.json`
- `release/ml_pure_runtime.env` on `PUBLISH` only

Published artifacts under `ml_pipeline_2/artifacts/published_models/<model_group>`:
- `model/model.joblib`
- `config/profiles/<profile_id>/threshold_report.json`
- `config/profiles/<profile_id>/training_report.json`
- publish/run reports

The runtime bundle contains:
- Stage 1 package
- Stage 2 package
- Stage 3 recipe packages
- recipe catalog payload
- runtime gate order
- staged runtime metadata

The runtime policy contains:
- Stage 1 threshold
- Stage 2 CE and PE thresholds plus minimum edge
- Stage 3 threshold and recipe margin
- runtime prefilter gate IDs
- `block_expiry`

## Design Rules

Current design rules for the package:
- the staged manifest is explicit; hidden training defaults are avoided in the supported lane
- direct dependencies on the old `ml_pipeline` package are not allowed
- staged publish is gated by holdout and combined hard-gate checks
- staged research may complete early with `completion_mode=stage2_signal_check_failed|stage1_cv_gate_failed|stage2_cv_gate_failed`; those summaries still write `publish_assessment=HOLD`, `cv_prechecks`, and partial `stage_artifacts`
- setup work is treated as first-class run state; staged runs emit `prep_start` / `prep_done` events for support load, oracle build, and stage preparation before stage training begins
- runtime handoff uses run ID and model group, not ad hoc local model paths
- if `runtime.block_expiry=true`, staged training filters expiry-day rows before oracle construction and stage labeling so training and runtime semantics stay aligned

## Source Inventory

For a full file-by-file map of every Python file in `src/ml_pipeline_2`, use `detailed_design.md`.

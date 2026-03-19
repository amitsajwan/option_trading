# ml_pipeline_2 Detailed Design

This document is the file-by-file design map for `ml_pipeline_2`.

It has two jobs:
- describe the supported staged training and publish flow end to end
- inventory every Python file under `src/ml_pipeline_2`

## Supported Staged Flow

The supported release path is the staged manifest-driven lane.

Execution sequence:

1. `src/ml_pipeline_2/run_staged_release.py`
   - CLI entrypoint for train, assess, publish, and optional remote sync.
2. `src/ml_pipeline_2/staged/publish.py`
   - release orchestration for staged runs.
3. `src/ml_pipeline_2/run_research.py`
   - shared CLI entrypoint for manifest validation and research execution.
4. `src/ml_pipeline_2/contracts/manifests.py`
   - resolves and validates the manifest contract.
5. `src/ml_pipeline_2/experiment_control/runner.py`
   - creates the run root and dispatches by experiment kind.
6. `src/ml_pipeline_2/staged/pipeline.py`
   - loads parquet, builds oracle labels, trains Stage 1 / 2 / 3, selects policy, scores holdout, and writes `summary.json`.
7. `src/ml_pipeline_2/staged/runtime_contract.py`
   - validates the staged runtime bundle and policy contract.
8. `src/ml_pipeline_2/publishing/publish.py`
   - resolves the published-model root used by the staged publisher and downstream resolver.
9. `src/ml_pipeline_2/publishing/release.py`
   - handles optional bucket sync for a published model group.
10. downstream runtime consumes the published artifacts through `strategy_app`, not through `ml_pipeline_2` itself.

## Inputs

Required local datasets:
- `.data/ml_pipeline/parquet_data/snapshots`
- `.data/ml_pipeline/parquet_data/snapshots_ml_flat`
- `.data/ml_pipeline/parquet_data/stage1_entry_view`
- `.data/ml_pipeline/parquet_data/stage2_direction_view`
- `.data/ml_pipeline/parquet_data/stage3_recipe_view`

Supported checked-in manifest:
- `configs/research/staged_dual_recipe.default.json`

## Outputs

Research outputs:
- `ml_pipeline_2/artifacts/research/<run_id>/summary.json`
- `ml_pipeline_2/artifacts/research/<run_id>/stages/stage1/model.joblib`
- `ml_pipeline_2/artifacts/research/<run_id>/stages/stage2/model.joblib`
- `ml_pipeline_2/artifacts/research/<run_id>/stages/stage3/recipes/<recipe_id>/model.joblib`
- `ml_pipeline_2/artifacts/research/<run_id>/release/ml_pure_runtime.env`

Published outputs:
- `ml_pipeline_2/artifacts/published_models/<model_group>/model/model.joblib`
- `ml_pipeline_2/artifacts/published_models/<model_group>/config/profiles/<profile_id>/threshold_report.json`
- `ml_pipeline_2/artifacts/published_models/<model_group>/config/profiles/<profile_id>/training_report.json`

## Source Inventory

Status labels used below:
- `supported`: part of the primary staged release lane
- `secondary`: useful and still maintained, but not the main operator path
- `infra`: package glue or export surface

### Package Root

- `src/ml_pipeline_2/__init__.py` - package export surface. Status: `infra`.
- `src/ml_pipeline_2/run_research.py` - CLI to validate manifests, print resolved config, and start research runs. Status: `supported`.
- `src/ml_pipeline_2/run_staged_release.py` - CLI for the supported staged train-plus-publish flow. Status: `supported`.
- `src/ml_pipeline_2/run_publish_model.py` - CLI to publish a completed staged run without retraining. Status: `supported`.
- `src/ml_pipeline_2/run_background_job.py` - CLI wrapper for detached background-job launch metadata. Status: `secondary`.

### `staged`

- `src/ml_pipeline_2/staged/__init__.py` - staged package export surface and lazy imports. Status: `infra`.
- `src/ml_pipeline_2/staged/pipeline.py` - core staged orchestration, oracle construction, training, policy search, holdout scoring, and summary writing. Status: `supported`.
- `src/ml_pipeline_2/staged/publish.py` - staged publish, release assessment, bundle writing, env handoff writing, and optional bucket sync. Status: `supported`.
- `src/ml_pipeline_2/staged/registries.py` - registries for staged views, labelers, trainers, and policies. Status: `supported`.
- `src/ml_pipeline_2/staged/recipes.py` - fixed staged recipe catalog definitions and lookup helpers. Status: `supported`.
- `src/ml_pipeline_2/staged/runtime_contract.py` - staged runtime bundle and runtime policy validation. Status: `supported`.

### `contracts`

- `src/ml_pipeline_2/contracts/__init__.py` - public contract exports. Status: `infra`.
- `src/ml_pipeline_2/contracts/types.py` - shared enums, dataclasses, and type-level constants. Status: `supported`.
- `src/ml_pipeline_2/contracts/manifests.py` - staged manifest schema validation, path resolution, and experiment-kind dispatch constraints. Status: `supported`.

### `publishing`

- `src/ml_pipeline_2/publishing/__init__.py` - publishing package export surface and lazy imports. Status: `infra`.
- `src/ml_pipeline_2/publishing/publish.py` - published-model root resolution and shared path helpers. Status: `supported`.
- `src/ml_pipeline_2/publishing/release.py` - optional bucket sync helper for published model groups. Status: `supported`.
- `src/ml_pipeline_2/publishing/resolver.py` - resolves published artifacts by run ID and model group for downstream consumers. Status: `supported`.

### `evaluation`

- `src/ml_pipeline_2/evaluation/__init__.py` - evaluation package exports. Status: `infra`.
- `src/ml_pipeline_2/evaluation/stage_metrics.py` - reusable predictive and utility metrics plus promotion-gate helpers. Status: `secondary`.
- `src/ml_pipeline_2/evaluation/direction.py` - direction-stage evaluation facade and diagnostics. Status: `secondary`.
- `src/ml_pipeline_2/evaluation/promotion.py` - promotion ladder and decision-payload helpers. Status: `secondary`.

### `inference_contract`

- `src/ml_pipeline_2/inference_contract/__init__.py` - inference package exports. Status: `infra`.
- `src/ml_pipeline_2/inference_contract/predict.py` - offline scoring against saved model packages with feature-contract validation. Status: `supported`.

### `experiment_control`

- `src/ml_pipeline_2/experiment_control/__init__.py` - experiment-control exports. Status: `infra`.
- `src/ml_pipeline_2/experiment_control/runner.py` - run-root creation, manifest execution, and experiment-kind dispatch. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/state.py` - run context and timestamp helpers for persisted run state. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/background.py` - detached background-job metadata and storage helpers. Status: `secondary`.

### `scenario_flows`

- `src/ml_pipeline_2/scenario_flows/__init__.py` - reserved namespace for scenario-specific orchestration modules. Status: `infra`.

### `catalog`

- `src/ml_pipeline_2/catalog/__init__.py` - catalog package exports. Status: `infra`.
- `src/ml_pipeline_2/catalog/research_defaults.py` - default external data roots, default staged recipes, and checked-in staged manifest defaults. Status: `secondary`.
- `src/ml_pipeline_2/catalog/models.py` - model catalog definitions and lookup helpers. Status: `supported`.
- `src/ml_pipeline_2/catalog/feature_sets.py` - named feature-set definitions for staged and shared lanes. Status: `supported`.
- `src/ml_pipeline_2/catalog/feature_profiles.py` - feature-profile filtering and exclusions. Status: `supported`.

### `dataset_windowing`

- `src/ml_pipeline_2/dataset_windowing/__init__.py` - dataset-windowing exports. Status: `infra`.
- `src/ml_pipeline_2/dataset_windowing/frames.py` - parquet loading, trade-date normalization, date slicing, and path-overlap helpers. Status: `supported`.

### `labeling`

- `src/ml_pipeline_2/labeling/__init__.py` - labeling package exports. Status: `infra`.
- `src/ml_pipeline_2/labeling/prepare.py` - timestamp ordering, expiry and VIX context derivation, and snapshot-frame preparation. Status: `supported`.
- `src/ml_pipeline_2/labeling/engine.py` - main label-construction engine and effective label config. Status: `supported`.
- `src/ml_pipeline_2/labeling/event_sampling.py` - event sampling helpers shared by secondary labeling paths. Status: `secondary`.
- `src/ml_pipeline_2/labeling/dealer_proxy.py` - dealer-proxy feature derivation helpers. Status: `supported`.
- `src/ml_pipeline_2/labeling/regime.py` - regime feature derivation and summary helpers. Status: `supported`.

### `model_search`

- `src/ml_pipeline_2/model_search/__init__.py` - model-search exports. Status: `infra`.
- `src/ml_pipeline_2/model_search/features.py` - feature-column selection and label-column exclusion rules. Status: `supported`.
- `src/ml_pipeline_2/model_search/walk_forward.py` - walk-forward day-fold construction. Status: `supported`.
- `src/ml_pipeline_2/model_search/event_purge.py` - purge-mode normalization and event-overlap purge logic. Status: `supported`.
- `src/ml_pipeline_2/model_search/metrics.py` - shared drawdown and profit-factor helpers. Status: `supported`.
- `src/ml_pipeline_2/model_search/search.py` - main training cycle, preprocessing, model fitting, CV evaluation, and package creation. Status: `supported`.

## Package-Level Sequence Details

### 1. Manifest Resolution

Files:
- `src/ml_pipeline_2/run_research.py`
- `src/ml_pipeline_2/contracts/manifests.py`
- `src/ml_pipeline_2/experiment_control/runner.py`

Responsibilities:
- parse CLI arguments
- load the checked-in manifest or a custom manifest
- validate experiment kind, windows, views, labels, training config, runtime config, and hard gates
- create a run directory and persist the resolved configuration

### 2. Dataset Loading and Label Construction

Files:
- `src/ml_pipeline_2/dataset_windowing/frames.py`
- `src/ml_pipeline_2/labeling/prepare.py`
- `src/ml_pipeline_2/labeling/engine.py`
- `src/ml_pipeline_2/staged/recipes.py`
- `src/ml_pipeline_2/staged/registries.py`

Responsibilities:
- load stage views and support datasets from local parquet
- normalize timestamps and trade dates
- derive expiry and regime context
- build staged oracle labels and utility targets
- apply stage-specific labelers

### 3. Model Search and Holdout Evaluation

Files:
- `src/ml_pipeline_2/model_search/features.py`
- `src/ml_pipeline_2/model_search/walk_forward.py`
- `src/ml_pipeline_2/model_search/event_purge.py`
- `src/ml_pipeline_2/model_search/metrics.py`
- `src/ml_pipeline_2/model_search/search.py`
- `src/ml_pipeline_2/inference_contract/predict.py`
- `src/ml_pipeline_2/staged/pipeline.py`

Responsibilities:
- select allowed feature columns
- build walk-forward folds
- apply purge and embargo logic
- fit candidate models
- select winning packages
- score validation and holdout windows
- compute publish-gate summaries

### 4. Publish and Runtime Handoff

Files:
- `src/ml_pipeline_2/staged/publish.py`
- `src/ml_pipeline_2/staged/runtime_contract.py`
- `src/ml_pipeline_2/publishing/publish.py`
- `src/ml_pipeline_2/publishing/release.py`
- `src/ml_pipeline_2/publishing/resolver.py`

Responsibilities:
- reject non-publishable staged runs
- build the staged runtime bundle and runtime policy
- write the published-model layout
- write `release/ml_pure_runtime.env`
- optionally sync the published group to a bucket
- support downstream resolution by run ID and model group

## Files Outside This Package But Relevant

These files are intentionally not moved into `ml_pipeline_2/src` because they belong to other systems:

- `docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md` - upstream parquet build lane
- `docs/GCP_BOOTSTRAP_RUNBOOK.md` - GCP bootstrap and infra lane
- `docs/GCP_DEPLOYMENT.md` - runtime deployment and cutover lane
- `snapshot_app/...` - live and historical snapshot production
- `strategy_app/...` - live runtime consumption of the published bundle

Those files matter to the full system, but they are not owned by the ML training package itself.

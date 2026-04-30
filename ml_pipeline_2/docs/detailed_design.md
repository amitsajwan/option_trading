# ml_pipeline_2 Detailed Design

This document is the file-by-file design map for `ml_pipeline_2`.

It has two jobs:

- describe the current supported staged flow end to end
- inventory the major Python entrypoints and package areas under `src/ml_pipeline_2`

## Current Supported Flow

The package now supports four closely related execution shapes:

1. single staged research run
2. staged release
3. staged grid
4. campaign / factory orchestration

All of them are manifest-driven and all ultimately dispatch into the same staged training core in `staged/pipeline.py`.

## Execution Sequence for a Single Staged Run

1. `src/ml_pipeline_2/run_research.py`
   - CLI entrypoint for manifest validation, resolved-config print, and research execution.
2. `src/ml_pipeline_2/contracts/manifests.py`
   - resolves and validates the manifest contract.
3. `src/ml_pipeline_2/experiment_control/runner.py`
   - creates the run root, writes lifecycle state, and dispatches by experiment kind.
4. `src/ml_pipeline_2/staged/pipeline.py`
   - loads parquet, builds oracle labels, applies Stage 1 / Stage 2 filters, runs the Stage 2 signal precheck, trains Stage 1 / 2 / 3, selects policy, scores holdout, and writes `summary.json`.
5. `src/ml_pipeline_2/staged/publish.py`
   - used only when the execution path includes release/publish work.

## Current Inputs

Expected local staged parquet root:

- `.data/ml_pipeline/parquet_data`

Common datasets:

- `snapshots`
- `snapshots_ml_flat`
- `snapshots_ml_flat_v2`
- `stage1_entry_view`
- `stage1_entry_view_v2`
- `stage2_direction_view`
- `stage2_direction_view_v2`
- `stage3_recipe_view`
- `stage3_recipe_view_v2`

Candidate datasets such as `*_v3_candidate` are valid when the staged registries expose matching view IDs and the manifest points at them.

## Current Outputs

Single research outputs:

- `ml_pipeline_2/artifacts/research/<run_id>/summary.json`
- `ml_pipeline_2/artifacts/research/<run_id>/resolved_config.json`
- `ml_pipeline_2/artifacts/research/<run_id>/state.jsonl`
- `ml_pipeline_2/artifacts/research/<run_id>/run_status.json`
- stage model packages and training reports

Grid outputs:

- `ml_pipeline_2/artifacts/research/<grid_run_id>/grid_status.json`
- `ml_pipeline_2/artifacts/research/<grid_run_id>/grid_summary.json`
- generated manifests under `manifests/`
- child run roots under `runs/`

Campaign outputs:

- `ml_pipeline_2/artifacts/campaign_runs/<campaign_id>/workflow_status.json`
- lane roots under `lanes/...`

Release outputs:

- `release/assessment.json`
- `release/release_summary.json`
- `release/ml_pure_runtime.env` on publishable runs only

## Important Current Runtime Behavior

### Setup Progress Events

Long staged runs now emit setup progress events before model training begins.

Expected setup events in `state.jsonl`:

- `prep_start` / `prep_done` for `support_load`
- `prep_start` / `prep_done` for `oracle_build`
- `prep_start` / `prep_done` for `stage_prepare`

This is important because the expensive setup path can be active long before the first `stage_start` event.

### Stage-Scoped Filters and Redesign Controls

The manifest contract currently supports:

- `training.stage1_session_filter`
- `training.stage2_session_filter`
- `training.stage2_label_filter`
- `training.stage2_target_redesign`

These are validated in `contracts/manifests.py` and applied inside `staged/pipeline.py`.

### Preflight

`run_staged_data_preflight.py` is the package-level dataset contract checker.

It validates:

- support/view parity
- feature-set resolution
- missing-rate expectations
- Stage 2 temporal validity for `ctx_am_*` and `vel_*` on Stage 2-style views

## Source Inventory

Status labels used below:

- `supported`: part of the main current package surface
- `secondary`: maintained, but not the primary operator path
- `infra`: glue, exports, or helpers

### Package Root CLIs

- `src/ml_pipeline_2/run_research.py` - validate a manifest, print resolved config, and execute one research run. Status: `supported`.
- `src/ml_pipeline_2/run_staged_release.py` - staged train-plus-release CLI. Status: `supported`.
- `src/ml_pipeline_2/run_publish_model.py` - publish an existing completed run. Status: `supported`.
- `src/ml_pipeline_2/run_staged_grid.py` - execute a staged grid over generated child manifests. Status: `supported`.
- `src/ml_pipeline_2/run_training_campaign.py` - execute a campaign spec that generates lane roots. Status: `supported`.
- `src/ml_pipeline_2/run_training_factory.py` - execute a higher-level factory spec. Status: `supported`.
- `src/ml_pipeline_2/run_staged_data_preflight.py` - dataset parity and feature-coverage checks for staged manifests. Status: `supported`.
- `src/ml_pipeline_2/run_stage2_feature_signal_diagnostic.py` - Stage 2 feature-signal memo runner. Status: `secondary`.
- `src/ml_pipeline_2/run_stage2_calibration_diagnostic.py` - Stage 2 calibration analysis CLI. Status: `secondary`.
- `src/ml_pipeline_2/run_stage2_side_rebalance_diagnostic.py` - Stage 2 side rebalance analysis CLI. Status: `secondary`.
- `src/ml_pipeline_2/run_stage12_counterfactual.py` - Stage 1+2 counterfactual analysis CLI. Status: `secondary`.
- `src/ml_pipeline_2/run_stage12_confidence_execution.py` - confidence execution analysis CLI. Status: `secondary`.
- `src/ml_pipeline_2/run_stage12_confidence_execution_policy.py` - confidence execution policy CLI. Status: `secondary`.
- `src/ml_pipeline_2/run_stage12_skew_diagnostic.py` - Stage 1+2 skew analysis CLI. Status: `secondary`.
- `src/ml_pipeline_2/run_background_job.py` - detached background-job launch wrapper. Status: `secondary`.

### `staged`

- `src/ml_pipeline_2/staged/pipeline.py` - core staged orchestration, filter application, label building, model search, diagnostics, policy selection, and summary writing. Status: `supported`.
- `src/ml_pipeline_2/staged/publish.py` - release assessment, bundle writing, publish flow, and optional sync. Status: `supported`.
- `src/ml_pipeline_2/staged/grid.py` - staged grid expansion, child-run execution, and result collation. Status: `supported`.
- `src/ml_pipeline_2/staged/registries.py` - staged view, labeler, trainer, and policy registries. Status: `supported`.
- `src/ml_pipeline_2/staged/recipes.py` - fixed staged recipe catalog definitions. Status: `supported`.
- `src/ml_pipeline_2/staged/runtime_contract.py` - runtime bundle and runtime policy validation. Status: `supported`.
- `src/ml_pipeline_2/staged/stage2_feature_signal.py` - Stage 2 signal diagnostic implementation. Status: `secondary`.
- `src/ml_pipeline_2/staged/stage2_calibration.py` - Stage 2 calibration diagnostic implementation. Status: `secondary`.
- `src/ml_pipeline_2/staged/stage2_side_rebalance.py` - Stage 2 side rebalance diagnostic implementation. Status: `secondary`.
- `src/ml_pipeline_2/staged/skew_diagnostic.py` - Stage 12 skew analysis helpers. Status: `secondary`.
- `src/ml_pipeline_2/staged/counterfactual.py` - counterfactual scoring helpers. Status: `secondary`.
- `src/ml_pipeline_2/staged/confidence_execution.py` - confidence execution analysis helpers. Status: `secondary`.
- `src/ml_pipeline_2/staged/confidence_execution_policy.py` - confidence execution policy helpers. Status: `secondary`.
- `src/ml_pipeline_2/staged/dual_side_policy.py` - dual-side policy analysis helpers. Status: `secondary`.
- `src/ml_pipeline_2/staged/stage2_policy_core.py` - Stage 2 policy evaluation helpers shared by the pipeline and diagnostics. Status: `supported`.
- `src/ml_pipeline_2/staged/stage2_diagnostic_common.py` - shared Stage 2 diagnostic data assembly. Status: `secondary`.
- `src/ml_pipeline_2/staged/robustness.py` - grid-level robustness probing helpers. Status: `secondary`.

### `contracts`

- `src/ml_pipeline_2/contracts/manifests.py` - schema validation, path resolution, and training/filter contract validation. Status: `supported`.
- `src/ml_pipeline_2/contracts/types.py` - shared typed payloads and constants. Status: `supported`.

### `catalog`

- `src/ml_pipeline_2/catalog/models.py` - model catalog definitions. Status: `supported`.
- `src/ml_pipeline_2/catalog/feature_sets.py` - named feature-set definitions, including newer Stage 2 variants. Status: `supported`.
- `src/ml_pipeline_2/catalog/feature_profiles.py` - feature-profile include/exclude logic. Status: `supported`.
- `src/ml_pipeline_2/catalog/research_defaults.py` - checked-in defaults and path helpers. Status: `secondary`.

### `dataset_windowing`

- `src/ml_pipeline_2/dataset_windowing/frames.py` - parquet loading, timestamp normalization, and date slicing. Status: `supported`.

### `labeling`

- `src/ml_pipeline_2/labeling/prepare.py` - snapshot-frame preparation. Status: `supported`.
- `src/ml_pipeline_2/labeling/engine.py` - label-construction engine. Status: `supported`.
- `src/ml_pipeline_2/labeling/regime.py` - regime feature derivation. Status: `supported`.
- `src/ml_pipeline_2/labeling/dealer_proxy.py` - dealer-proxy feature helpers. Status: `supported`.
- `src/ml_pipeline_2/labeling/event_sampling.py` - event sampling helpers. Status: `secondary`.

### `model_search`

- `src/ml_pipeline_2/model_search/features.py` - feature-column selection and exclusions. Status: `supported`.
- `src/ml_pipeline_2/model_search/walk_forward.py` - walk-forward day-fold construction. Status: `supported`.
- `src/ml_pipeline_2/model_search/event_purge.py` - purge and embargo logic. Status: `supported`.
- `src/ml_pipeline_2/model_search/metrics.py` - shared search-time metric helpers. Status: `supported`.
- `src/ml_pipeline_2/model_search/search.py` - preprocessing, fitting, CV evaluation, and package creation. Status: `supported`.

### `inference_contract`

- `src/ml_pipeline_2/inference_contract/predict.py` - offline scoring against saved packages, including `direction_or_no_trade` package handling. Status: `supported`.

### `evaluation`

- `src/ml_pipeline_2/evaluation/stage_metrics.py` - predictive and utility metrics plus gate helpers. Status: `secondary`.
- `src/ml_pipeline_2/evaluation/direction.py` - direction-stage evaluation facade. Status: `secondary`.
- `src/ml_pipeline_2/evaluation/promotion.py` - promotion decision helpers. Status: `secondary`.

### `experiment_control`

- `src/ml_pipeline_2/experiment_control/runner.py` - run-root creation, state emission, and experiment-kind dispatch. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/state.py` - run context and timestamp helpers. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/coordination.py` - output-root locking and coordination helpers. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/registry.py` - run-root registration helpers. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/status.py` - status artifact helpers. Status: `supported`.
- `src/ml_pipeline_2/experiment_control/background.py` - detached background-job metadata. Status: `secondary`.

### `campaign`

- `src/ml_pipeline_2/campaign/spec.py` - campaign spec validation and typed parsing. Status: `supported`.
- `src/ml_pipeline_2/campaign/generator.py` - lane generation and expansion. Status: `supported`.
- `src/ml_pipeline_2/campaign/runner.py` - campaign execution and workflow status writing. Status: `supported`.

### `factory`

- `src/ml_pipeline_2/factory/spec.py` - factory-spec validation and parsing. Status: `supported`.
- `src/ml_pipeline_2/factory/launcher.py` - factory child-job launch helpers. Status: `supported`.
- `src/ml_pipeline_2/factory/monitor.py` - factory monitoring and status aggregation. Status: `supported`.
- `src/ml_pipeline_2/factory/runner.py` - top-level factory execution. Status: `supported`.

### `publishing`

- `src/ml_pipeline_2/publishing/publish.py` - published-model root resolution. Status: `supported`.
- `src/ml_pipeline_2/publishing/release.py` - optional bucket sync for published groups. Status: `supported`.
- `src/ml_pipeline_2/publishing/resolver.py` - downstream published-artifact resolution by run and model group. Status: `supported`.

### `scenario_flows`

- reserved namespace for scenario-specific orchestration modules. Status: `infra`.

## Package-Level Sequence Details

### 1. Manifest / Spec Resolution

Files:

- `run_research.py`
- `run_staged_grid.py`
- `run_training_campaign.py`
- `run_training_factory.py`
- `contracts/manifests.py`
- `campaign/spec.py`
- `factory/spec.py`

Responsibilities:

- validate config shape
- resolve paths and windows
- normalize training overrides
- create deterministic child manifests where needed

### 2. Dataset Loading and Label Construction

Files:

- `dataset_windowing/frames.py`
- `labeling/prepare.py`
- `labeling/engine.py`
- `staged/recipes.py`
- `staged/registries.py`
- `staged/pipeline.py`

Responsibilities:

- load support and stage views
- normalize timestamps and trade dates
- build staged oracle labels and utility targets
- apply stage-scoped session and label filters

### 3. Model Search and Diagnostics

Files:

- `model_search/*`
- `staged/pipeline.py`
- `staged/stage2_*`
- `staged/skew_diagnostic.py`
- `staged/confidence_execution*.py`

Responsibilities:

- construct walk-forward folds
- fit candidate models
- apply CV gates
- run Stage 2 signal and calibration diagnostics
- compute policy and holdout summaries

### 4. Orchestration and Ranking

Files:

- `staged/grid.py`
- `campaign/generator.py`
- `campaign/runner.py`
- `factory/runner.py`

Responsibilities:

- scenario expansion
- child-run ordering and reuse
- lane execution
- workflow and campaign status collation

### 5. Publish and Runtime Handoff

Files:

- `staged/publish.py`
- `staged/runtime_contract.py`
- `publishing/*`

Responsibilities:

- reject non-publishable runs in publish-only paths
- write release assessment artifacts
- build the runtime bundle and policy
- write published-model layouts and optional env handoff

## Files Outside This Package But Relevant

These remain outside `ml_pipeline_2/src` because they belong to other systems:

- `docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- `docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`
- `docs/runbooks/GCP_DEPLOYMENT.md`
- `snapshot_app/...`
- `strategy_app/...`

Those files matter to the full system, but they are not owned by the ML training package itself.

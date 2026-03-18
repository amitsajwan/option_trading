# Architecture

## Purpose

`ml_pipeline_2` is the bounded, research-only workspace for frozen-feature ML experiments.

Design goals:
- config-driven experimentation
- explicit ownership by package boundary
- no model-building dependency on `ml_pipeline`
- restart-safe artifact writing for long or failure-prone runs
- explicit staged training across entry, direction, and recipe-selection steps
- first-class publication of runtime-usable staged `ml_pure` bundles

Operational runbook:
- [`docs/ubuntu_gcp_runbook.md`](docs/ubuntu_gcp_runbook.md)

## Current Supported Flows

Research scenarios:
- `staged_dual_recipe_v1`
- `phase2_label_sweep_v1`
- `fo_expiry_aware_recovery_v1`

Primary operator flow:
- `run_staged_release.py`
- `run_publish_model.py`

Legacy utility flows:
- `run_move_detector_quick.py`
- `run_direction_from_move_quick.py`

The staged manifest-driven runner is the supported release lane for this branch. The quick runners remain in-tree as bounded research utilities and are not the primary operator path.

## Bounded Contexts

### `contracts`

Owns:
- shared dataclasses
- label-target enums
- manifest kinds
- manifest validation and resolution

Key files:
- [contracts/types.py](src/ml_pipeline_2/contracts/types.py)
- [contracts/manifests.py](src/ml_pipeline_2/contracts/manifests.py)

### `catalog`

Owns:
- built-in feature profiles
- built-in feature sets
- built-in model specs
- checked-in research defaults

Current tuned tree presets:
- XGBoost:
  - `xgb_shallow`
  - `xgb_balanced`
  - `xgb_regularized`
  - `xgb_deep_v1`
  - `xgb_deep_slow_v1`
- LightGBM:
  - `lgbm_fast`
  - `lgbm_dart`
  - `lgbm_large_v1`
  - `lgbm_large_dart_v1`

Rule:
- tuned models are preset-based catalog entries, not free-form manifest hyperparameters
- XGBoost and LightGBM stay at `n_jobs=1`
- scaling is handled by outer experiment parallelism, not per-model thread expansion

Key files:
- [catalog/feature_profiles.py](src/ml_pipeline_2/catalog/feature_profiles.py)
- [catalog/feature_sets.py](src/ml_pipeline_2/catalog/feature_sets.py)
- [catalog/models.py](src/ml_pipeline_2/catalog/models.py)

### `dataset_windowing`

Owns:
- parquet loading
- timestamp normalization
- explicit date filtering
- path overlap checks

Key file:
- [dataset_windowing/frames.py](src/ml_pipeline_2/dataset_windowing/frames.py)

### `labeling`

Owns:
- futures labeling engine
- label lineage
- snapshot-style frame preparation
- regime feature backfill
- Stage 1 move labels

Current important outputs:
- directional labels:
  - `long_*`
  - `short_*`
  - `ce_*`
  - `pe_*`
- move-detector labels:
  - `move_label_valid`
  - `move_label`
  - `move_first_hit_side`
  - `move_event_end_ts`
  - `move_barrier_upper_return`
  - `move_barrier_lower_return`

These move fields are the contract between Stage 1 and Stage 2.

Key files:
- [labeling/engine.py](src/ml_pipeline_2/labeling/engine.py)
- [labeling/prepare.py](src/ml_pipeline_2/labeling/prepare.py)

### `model_search`

Owns:
- feature selection from labeled frames
- feature-set filtering
- model construction
- walk-forward evaluation
- final model package creation

Current prediction modes:
- directional:
  - trains separate `ce` and `pe` models
  - outputs `ce_prob` and `pe_prob`
- move:
  - trains a single `move` model
  - outputs `move_prob`
- direction_up:
  - trains a single conditional direction model
  - outputs `direction_up_prob`

Rule:
- Stage 1 `move_barrier_hit` uses standard model-search infrastructure, but it does not use trade-utility scoring because that objective assumes directional CE/PE execution.

Key files:
- [model_search/features.py](src/ml_pipeline_2/model_search/features.py)
- [model_search/search.py](src/ml_pipeline_2/model_search/search.py)
- [model_search/walk_forward.py](src/ml_pipeline_2/model_search/walk_forward.py)

### `inference_contract`

Owns:
- model package loading
- required feature checks
- offline probability scoring from a frame

Current package modes:
- directional packages require `models["ce"]` and `models["pe"]`
- move packages require `models["move"]`
- direction packages require `models["direction"]`

Key file:
- [inference_contract/predict.py](src/ml_pipeline_2/inference_contract/predict.py)

### `evaluation`

Owns:
- Stage A predictive quality
- Stage B futures utility
- Stage C mapping diagnostics
- promotion summaries

Current limitation:
- `evaluation` is still directional-first
- staged release quality is summarized in `staged/pipeline.py` via holdout metrics and hard gates
- the legacy quick runners still emit their own standalone summaries

Key files:
- [evaluation/stage_metrics.py](src/ml_pipeline_2/evaluation/stage_metrics.py)
- [evaluation/direction.py](src/ml_pipeline_2/evaluation/direction.py)

### `staged`

Owns:
- staged oracle target construction
- Stage 1 / Stage 2 / Stage 3 orchestration
- staged policy search and hard-gate assessment
- staged runtime bundle and policy publication

Key files:
- [staged/pipeline.py](src/ml_pipeline_2/staged/pipeline.py)
- [staged/publish.py](src/ml_pipeline_2/staged/publish.py)
- [staged/registries.py](src/ml_pipeline_2/staged/registries.py)
- [staged/recipes.py](src/ml_pipeline_2/staged/recipes.py)

### `experiment_control`

Owns:
- run output root creation
- resolved-config persistence
- manifest hash persistence
- `state.jsonl`
- scenario dispatch
- detached background job metadata for long matrix runs

Key files:
- [experiment_control/runner.py](src/ml_pipeline_2/experiment_control/runner.py)
- [experiment_control/state.py](src/ml_pipeline_2/experiment_control/state.py)
- [experiment_control/background.py](src/ml_pipeline_2/experiment_control/background.py)

### `publishing`

Owns:
- published-model layout under `artifacts/published_models`
- publish-time threshold and model contract generation
- run/latest publish reports
- run-id and model-group resolution for downstream consumers

Current V1 scope:
- resolve published staged and recovery artifacts for downstream consumers
- maintain run/latest publish reports under `artifacts/published_models`
- support staged runtime switch-by-run-id for `ml_pure`

Key files:
- [publishing/publish.py](src/ml_pipeline_2/publishing/publish.py)
- [publishing/resolver.py](src/ml_pipeline_2/publishing/resolver.py)
- [run_publish_model.py](src/ml_pipeline_2/run_publish_model.py)

### `scenario_flows`

Owns orchestration only:
- staged dual-recipe research
- phase-2 label sweep
- recovery research

Key files:
- [scenario_flows/staged_dual_recipe.py](src/ml_pipeline_2/scenario_flows/staged_dual_recipe.py)
- [scenario_flows/phase2_label_sweep.py](src/ml_pipeline_2/scenario_flows/phase2_label_sweep.py)
- [scenario_flows/fo_expiry_aware_recovery.py](src/ml_pipeline_2/scenario_flows/fo_expiry_aware_recovery.py)

## Dependency Rules

Intended dependency direction:
- `contracts` and `catalog` are foundational
- `dataset_windowing`, `labeling`, `model_search`, `inference_contract`, and `evaluation` depend on foundation layers
- `staged` composes labeling, model-search, and publish-facing contexts for the supported release lane
- `scenario_flows` orchestrates core contexts
- `experiment_control` dispatches into scenario flows
- `run_move_detector_quick.py` may compose existing bounded contexts, but it must not bypass them by duplicating model or label logic inline

Enforced expectations:
- no import of `ml_pipeline`
- scenario flows do not import each other

Boundary tests:
- [test_boundaries.py](tests/test_boundaries.py)

## Stage 1 Move Detector Design

Question answered:
- "Is there a meaningful move within the next horizon?"

Current label:
- `move_barrier_hit`
- `1` when either directional barrier resolves within the horizon
- `0` when both sides time-stop

Current saved direction clue:
- `move_first_hit_side`
- values: `up`, `down`, `none`, `invalid`

Why this split exists:
- move detection and direction are different learning problems
- the package previously forced directional CE/PE models even when the real question was `move` vs `no_move`
- the binary move detector is the front stage for later Stage 2 direction training

## Stage 2 Direction Design

Question answered:
- "Given that a move is likely, is the first resolved side up or down?"

Current label:
- `move_direction_up`
- train only on rows where `move_label == 1`
- target `1` when `move_first_hit_side == up`
- target `0` when `move_first_hit_side == down`

Current operator flow:
- Stage 2 consumes a completed Stage 1 run directory
- it reuses Stage 1 labeled artifacts instead of rebuilding labels
- it combines Stage 1 `move_prob` with Stage 2 `direction_up_prob` in the holdout summary

Current status:
- Stage 1 predictive quality is strong enough to continue
- Stage 2 direction quality is currently the weak link

## Configurability Rule

The flow we are currently building must be operable without editing Python for routine reruns.

For the supported staged lane, routine reruns should change only the checked-in staged manifest:
- [configs/research/staged_dual_recipe.default.json](configs/research/staged_dual_recipe.default.json)

The staged manifest owns:
- parquet root and support dataset
- train/valid/full-model/holdout windows
- per-stage model catalogs and feature-set catalogs
- per-stage policy grids
- runtime prefilter gate ids
- staged hard gates for publishability

Legacy quick-runner knobs remain configurable in JSON or CLI:
- move detector lane
- input parquet paths
- train/holdout windows
- ATR multiplier
- horizon
- minimum entry time
- feature profile
- one or more feature sets
- one or more model choices
- optional `max_experiments` cap
- CV windows
- threshold grid
- output root or explicit run directory
- resume toggle

Checked-in template:
- [configs/research/move_detector_quick.default.json](configs/research/move_detector_quick.default.json)

- direction-from-move lane
- Stage 1 run directory
- feature profile
- one or more feature sets
- one or more model choices
- optional `max_experiments` cap
- CV windows
- Stage 1 move threshold
- Stage 2 direction threshold grid
- cost-per-trade assumption for the combined summary
- output root or explicit run directory
- resume toggle

Checked-in template:
- [configs/research/direction_from_move_quick.default.json](configs/research/direction_from_move_quick.default.json)

## Restart-Safe Artifact Rule

Long or fragile runs must persist byproducts early enough to restart without recomputing everything.

The supported staged lane now writes:
- `resolved_config.json`
- `manifest_hash.txt`
- `state.jsonl`
- `stages/stage1/selection_model.joblib`
- `stages/stage1/model.joblib`
- `stages/stage2/selection_model.joblib`
- `stages/stage2/model.joblib`
- `stages/stage3/recipes/<recipe_id>/model.joblib`
- `stages/*/training_report.json`
- `stages/*/feature_contract.json`
- `summary.json`
- `release/assessment.json`
- `release/ml_pure_runtime.env`
- `release/release_summary.json`

Legacy quick-runner artifacts:
- move detector lane
- `resolved_config.json`
- `state.jsonl`
- `model_window_features_windowed.parquet`
- `holdout_features_windowed.parquet`
- `model_window_labeled.parquet`
- `holdout_labeled.parquet`
- `label_lineage.json`
- `training_report.json`
- `model.joblib`
- `holdout_probabilities.parquet`
- `holdout_predictions.csv`
- `summary.json`

- direction-from-move lane
- `resolved_config.json`
- `state.jsonl`
- `stage1_reference.json`
- `model_window_labeled.parquet`
- `holdout_labeled.parquet`
- `training_report.json`
- `model.joblib`
- `holdout_move_probabilities.parquet`
- `holdout_direction_probabilities.parquet`
- `holdout_direction_predictions.csv`
- per-threshold trade reports under `combined_holdout/`
- `summary.json`

Validation and hyper-tuning contract:
- both quick runners delegate model selection to `run_training_cycle_catalog`
- the searched feature-set/model grid is recorded in `training_report.json`
- CV slices are recorded in `training_report.json.cv_config`
- the chosen experiment is recorded in `training_report.json.best_experiment`

Recovery tuning matrix contract:
- matrix generation writes one resolved manifest per `(feature_set, primary_model)` combo
- combo launches may be capped with `max_parallel`
- pending combos are launched later with `run_recovery_matrix --launch-pending`
- invalid model or feature-set names fail before combo manifests are written

Current staged tuning workflow:
1. `recovery_matrix.tuning_1m_e2e.json`
   - 1 recipe
   - 1 feature set
   - tuned tree model sweep
   - `max_parallel=3`
2. `recovery_matrix.tuning_5m.json`
   - existing recovery recipe grid
   - feature-set sweep across `fo_expiry_aware_v2`, `fo_oi_pcr_momentum`, `fo_no_time_context`
   - same tuned tree model sweep
   - `max_parallel=3`

Resume behavior:
- operator supplies a fixed `run_dir`
- rerun with `--resume`
- completed stages are reused when their expected outputs exist
- resume is rejected if the saved `resolved_config.json` does not match the current config

## What Is Intentionally Out Of Scope

Not owned by `ml_pipeline_2` in this phase:
- production training flows
- live inference runtime
- old wrapper CLIs from `ml_pipeline`
- portfolio/risk management
- snapshot-stage-view generation

## Extension Rules

Use config edits for:
- windows
- recipe parameters already supported
- thresholds
- built-in model/feature-set selection
- move-detector run directory and resume behavior
- direction-from-move run directory and resume behavior

Add code only when introducing:
- new scenario kind
- new label family
- new feature engineering
- new feature-set definition
- new model family
- new multi-stage orchestration beyond the current quick runners

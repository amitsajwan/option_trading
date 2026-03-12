# Architecture

## Purpose

`ml_pipeline_2` is the bounded, research-only workspace for frozen-feature ML experiments.

Design goals:
- config-driven experimentation
- explicit ownership by package boundary
- no runtime dependency on `ml_pipeline`
- restart-safe artifact writing for long or failure-prone runs
- clear separation between Stage 1 move detection and later Stage 2 direction logic

Operational runbook:
- [`docs/ubuntu_gcp_runbook.md`](docs/ubuntu_gcp_runbook.md)

## Current Supported Flows

Research scenarios:
- `phase2_label_sweep_v1`
- `fo_expiry_aware_recovery_v1`

Operator utility flow:
- `run_move_detector_quick.py`
- `run_direction_from_move_quick.py`

The move detector is not a replacement for the manifest-driven research runner. It is a bounded operator tool for fast Stage 1 binary experiments while the full Stage 2 direction workflow is still being built.

## Bounded Contexts

### `contracts`

Owns:
- shared dataclasses
- label-target enums
- manifest kinds
- manifest validation and resolution

Key files:
- [contracts/types.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/contracts/types.py)
- [contracts/manifests.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/contracts/manifests.py)

### `catalog`

Owns:
- built-in feature profiles
- built-in feature sets
- built-in model specs
- checked-in research defaults

Key files:
- [catalog/feature_profiles.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/catalog/feature_profiles.py)
- [catalog/feature_sets.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py)
- [catalog/models.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/catalog/models.py)

### `dataset_windowing`

Owns:
- parquet loading
- timestamp normalization
- explicit date filtering
- path overlap checks

Key file:
- [dataset_windowing/frames.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/dataset_windowing/frames.py)

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
- [labeling/engine.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/labeling/engine.py)
- [labeling/prepare.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/labeling/prepare.py)

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
- [model_search/features.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/model_search/features.py)
- [model_search/search.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/model_search/search.py)
- [model_search/walk_forward.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/model_search/walk_forward.py)

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
- [inference_contract/predict.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/inference_contract/predict.py)

### `evaluation`

Owns:
- Stage A predictive quality
- Stage B futures utility
- Stage C mapping diagnostics
- promotion summaries

Current limitation:
- `evaluation` is still directional-first
- Stage 1 move detector quality is currently summarized by classification metrics in the quick runner, not by the promotion ladder
- Stage 2 direction-from-move is also summarized by the quick runner today

Key files:
- [evaluation/stage_metrics.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/evaluation/stage_metrics.py)
- [evaluation/direction.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/evaluation/direction.py)

### `experiment_control`

Owns:
- run output root creation
- resolved-config persistence
- manifest hash persistence
- `state.jsonl`
- scenario dispatch

Key files:
- [experiment_control/runner.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/experiment_control/runner.py)
- [experiment_control/state.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/experiment_control/state.py)

### `scenario_flows`

Owns orchestration only:
- phase-2 label sweep
- recovery research

Key files:
- [scenario_flows/phase2_label_sweep.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/scenario_flows/phase2_label_sweep.py)
- [scenario_flows/fo_expiry_aware_recovery.py](/c:/code/option_trading/ml_pipeline_2/src/ml_pipeline_2/scenario_flows/fo_expiry_aware_recovery.py)

## Dependency Rules

Intended dependency direction:
- `contracts` and `catalog` are foundational
- `dataset_windowing`, `labeling`, `model_search`, `inference_contract`, and `evaluation` depend on foundation layers
- `scenario_flows` orchestrates core contexts
- `experiment_control` dispatches into scenario flows
- `run_move_detector_quick.py` may compose existing bounded contexts, but it must not bypass them by duplicating model or label logic inline

Enforced expectations:
- no import of `ml_pipeline`
- scenario flows do not import each other

Boundary tests:
- [test_boundaries.py](/c:/code/option_trading/ml_pipeline_2/tests/test_boundaries.py)

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

For the move detector lane, change these in JSON or CLI:
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
- [configs/research/move_detector_quick.default.json](/c:/code/option_trading/ml_pipeline_2/configs/research/move_detector_quick.default.json)

For the direction-from-move lane, change these in JSON or CLI:
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
- [configs/research/direction_from_move_quick.default.json](/c:/code/option_trading/ml_pipeline_2/configs/research/direction_from_move_quick.default.json)

## Restart-Safe Artifact Rule

Long or fragile runs must persist byproducts early enough to restart without recomputing everything.

The move detector lane now writes:
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

The direction-from-move lane now writes:
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

Resume behavior:
- operator supplies a fixed `run_dir`
- rerun with `--resume`
- completed stages are reused when their expected outputs exist
- resume is rejected if the saved `resolved_config.json` does not match the current config

## What Is Intentionally Out Of Scope

Not owned by `ml_pipeline_2` in this phase:
- production training flows
- publishing and activation
- live inference runtime
- old wrapper CLIs from `ml_pipeline`
- full Stage 2 direction execution policy
- portfolio/risk management

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

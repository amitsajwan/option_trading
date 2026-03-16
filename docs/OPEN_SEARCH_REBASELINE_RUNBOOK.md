# Open-Search Rebaseline Runbook

This runbook is the active procedure for deterministic + ML rebaseline on snapshot schema `2.0`.

Legacy status for the current milestone:

- this runbook remains valid for offline/historical research flows
- it is not part of the supported fresh-machine `Live+Dashboard` runtime target
- offline entry-quality stages now resolve through `strategy_app.offline_ml`
- frozen parquet inputs still come from `.data/ml_pipeline/parquet_data`

## 1. Generate/Refresh Window Manifest

```powershell
python -m snapshot_app.historical.snapshot_batch_runner `
  --validate-only `
  --base .data/ml_pipeline/parquet_data `
  --window-manifest-out .run/window_manifest_latest.json
```

Mandatory manifest fields:

- `window_start`, `window_end`, `trading_days`
- `all_days_v2`, `schema_version`
- `generated_at`, `source_path`
- `formal_ready`, `exploratory_only`

## 2. Decide Run Mode

- Use exploratory mode when `formal_ready=false`.
- Use formal mode only when manifest is formal-ready.

Formal run must fail if any readiness condition is false:

- `all_days_v2=true`
- `schema_version=2.0`
- `trading_days>=150`

## 3. Exploratory Cycle

```powershell
python -m strategy_app.tools.open_search_rebaseline_cycle `
  --window-manifest .run/window_manifest_latest.json `
  --parquet-base .data/ml_pipeline/parquet_data `
  --output-root .run/open_search_rebaseline_exploratory
```

## 4. Formal Cycle

```powershell
python -m strategy_app.tools.open_search_rebaseline_cycle `
  --window-manifest .run/window_manifest_latest.json `
  --formal-run `
  --parquet-base .data/ml_pipeline/parquet_data `
  --output-root .run/open_search_rebaseline_formal `
  --require-positive-return `
  --max-champions 3
```

## 5. Optional Search-Space Controls

Use explicit lists to control matrix size:

```powershell
python -m strategy_app.tools.open_search_rebaseline_cycle `
  --window-manifest .run/window_manifest_latest.json `
  --formal-run `
  --parquet-base .data/ml_pipeline/parquet_data `
  --output-root .run/open_search_rebaseline_custom `
  --feature-profiles eq_core_snapshot_v1,eq_full_v1 `
  --label-profiles mfe15_gt_5_v1 `
  --segmentation-policies seg_regime_v1 `
  --model-families logreg_baseline_v1,lgbm_default_v1,lgbm_regularized_v1 `
  --threshold-policies fixed_060,segment_optimal,strategy_override_v1 `
  --max-champions 3
```

## 6. Expected Outputs

Cycle root:

- `cycle_summary.json`
- `manifest_meta.json`
- `split_boundaries.json`

Deterministic outputs:

- `deterministic/valid_registry.csv`
- `deterministic/holdout_registry.csv`
- `deterministic/valid_comparator.json`
- `deterministic/champion.json`

ML outputs:

- `ml/candidates/meta.json`
- `ml/experiments/experiment_registry.csv`
- `ml/replay_valid/evaluation_registry.csv`
- `ml/replay_holdout/evaluation_registry.csv`
- `ml/champions/champion_registry.json`
- `ml/champions/rejected_candidates.csv`

## 7. Triage Flow

1. If run fails before training:
   - check `window_manifest` readiness and split boundaries
   - confirm manifest hash in `manifest_meta.json`
2. If deterministic has no accepted candidate:
   - inspect `deterministic/valid_registry.csv` gate columns
   - tune search spec or gate settings explicitly
3. If ML has no champion:
   - inspect `ml/champions/rejected_candidates.csv`
   - identify dominant failing gates (`return`, `drawdown`, `trade_count`, `strategy_diversification`)
4. If output looks stuck:
   - verify process is still running and writing stage artifacts
   - check logs for repeated risk halt messages versus hard errors

## 8. Promotion Safety Reminder

Offline champion selection does not mean live enablement.

Runtime ML can be enabled only after:

1. offline formal gates pass
2. paper stage minimum days pass
3. shadow stage minimum days pass
4. capped-live constraints and guard file checks pass

## 9. Related Docs

- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [strategy_eval_architecture.md](strategy_eval_architecture.md)
- [strategy_catalog.md](strategy_catalog.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)

## 10. Latest Completed Formal Cycle (Reference)

Completed cycle:

- cycle id: `2021-01-01_2022-02-17_5e931d3969`
- output root: `.run/open_search_rebaseline_20260305_main/2021-01-01_2022-02-17_5e931d3969`
- formal run: `true`
- trained variants: `720`
- valid evaluated: `720`

Selected ML champion (hard-gate pass):

- `eq_structure_momentum_v1__mfe15_gt_5_v1__seg_regime_v1__lgbm_default_v1__fixed_060`
- holdout return: `+0.416966%`
- holdout max drawdown: `-1.305739%`
- holdout PF: `1.36176`
- holdout trades: `48`
- comparator deterministic return: `-0.869618%`

Reference artifacts:

- `cycle_summary.json`
- `ml/champions/champion_registry.json`
- `ml/replay_valid/evaluation_registry.csv`
- `ml/replay_holdout/evaluation_registry.csv`

## 11. Post-Eval Rollout Commands

Keep runtime ML off until paper/shadow minimum days are completed.

### Paper stage runtime (deterministic-only)

```powershell
python -m strategy_app.main --engine deterministic --rollout-stage paper --topic market:snapshot:v1
```

### Shadow stage runtime (deterministic-only)

```powershell
python -m strategy_app.main --engine deterministic --rollout-stage shadow --topic market:snapshot:v1
```

### Capped-live with ML entry gate (only after approvals)

Guard file must include:

- `approved_for_runtime=true`
- `offline_strict_positive_passed=true`
- `paper_days_observed>=10`
- `shadow_days_observed>=10`
- matching `approved_experiment_id`
- matching `approved_registry`

Command:

```powershell
python -m strategy_app.main `
  --engine deterministic `
  --topic market:snapshot:v1 `
  --rollout-stage capped_live `
  --position-size-multiplier 0.25 `
  --ml-entry-registry .run/open_search_rebaseline_20260305_main/2021-01-01_2022-02-17_5e931d3969/ml/replay_valid/evaluation_registry.csv `
  --ml-entry-experiment-id eq_structure_momentum_v1__mfe15_gt_5_v1__seg_regime_v1__lgbm_default_v1__fixed_060 `
  --ml-runtime-guard-file .run/ml_runtime_guard_approved.json
```

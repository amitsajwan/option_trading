# Strategy Evaluation Architecture

This document defines the current evaluation system for deterministic and ML under a single manifest/split contract.

Legacy status for the current milestone:

- this document describes historical replay/evaluation flows
- it is not part of the supported fresh-machine `Live+Dashboard` runtime target
- replay/eval orchestration is still a legacy lane
- offline entry-quality stages now resolve through `strategy_app.offline_ml`

## 1. Evaluation Planes

Two planes are active:

1. Replay execution plane (event-driven)
2. Open-search formal cycle plane (offline, manifest-gated)

Both planes use the same snapshot v2.0 source contract.

## 2. Replay Execution Plane

Core path:

1. UI/API creates replay run (`market_data_dashboard`).
2. `strategy_eval_orchestrator.main` consumes command and publishes historical snapshots to Redis historical topic.
3. `strategy_app.main` (historical consumer mode) evaluates snapshots through deterministic engine.
4. `persistence_app.main_strategy_consumer` writes votes/signals/positions for replay run.
5. Evaluation APIs reconstruct trades/equity from Mongo historical collections.

Key modules:

- `market_data_dashboard/strategy_evaluation_service.py`
- `strategy_eval_orchestrator/main.py`
- `strategy_app/runtime/redis_snapshot_consumer.py`
- `persistence_app/main_strategy_consumer.py`

## 3. Formal Open-Search Cycle Plane

Orchestrator:

- `strategy_app.tools.open_search_rebaseline_cycle`

Stages:

1. Manifest validation (`snapshot_app.historical.window_manifest`)
2. Deterministic open-matrix search (`strategy_app.tools.deterministic_open_matrix`)
3. Candidate dataset build (`strategy_app.offline_ml.entry_candidate_dataset`)
4. ML experiment matrix (`strategy_app.offline_ml.entry_quality_experiments`)
5. Replay valid and holdout (`strategy_app.offline_ml.entry_quality_replay_eval`)
6. Champion selection (`strategy_app.offline_ml.entry_quality_champion_select`)

## 4. Formal Cycle Contract

## Window manifest

Required fields:

- `window_start`, `window_end`, `trading_days`
- `all_days_v2`, `schema_version`
- `generated_at`, `source_path`
- derived: `manifest_hash`, `formal_ready`, `exploratory_only`

Formal run readiness:

- `all_days_v2=true`
- `schema_version=2.0`
- `trading_days>=150`

## Split contract

Single pre-committed day split from the manifest day list:

- train: first 60%
- valid: next 20%
- holdout(eval): final 20%

Deterministic and ML must consume identical split boundaries.

## 5. Deterministic Comparator Contract

Deterministic matrix evaluates candidate configs from risk/regime/router search space.

Baseline and candidate acceptance gates are applied in valid stage, then replayed on holdout.

Gate dimensions:

- return outperformance vs baseline
- optional strict-positive return
- drawdown multiple gate
- trade count gate

Comparator output:

- `deterministic/valid_comparator.json`

This comparator is the ML baseline for the same cycle.

## 6. ML Evaluation and Champion Contract

ML replay registry rows are scored against deterministic comparator with hard gates:

- `return_gate`: ML return beats deterministic return (+ optional min outperformance)
- `positive_return_gate`: ML return > 0 (formal strict mode)
- `max_drawdown_gate` and `drawdown_gate`
- `min_trades_gate` and `trade_count_gate`
- `strategy_diversification_gate`

Concentration metric:

- `top_strategy_return_share = abs(top_strategy_pnl) / sum(abs(strategy_pnl))`
- gate threshold default `<= 0.70`

Champion artifact:

- `ml/champions/champion_registry.json`

Rejected trace artifact:

- `ml/champions/rejected_candidates.csv`

## 7. Runtime Rollout Coupling

Offline champion pass does not auto-enable live ML.

Live ML enable requires:

- rollout stage `capped_live`
- `position_size_multiplier <= 0.25`
- guard file approval
- paper/shadow day minimums enforced by runtime checks

Enforcement paths:

- `strategy_app.main`
- `strategy_eval_orchestrator.main.validate_rollout_command`

## 8. Canonical Outputs by Stage

- Cycle root:
  - `cycle_summary.json`
  - `manifest_meta.json`
  - `split_boundaries.json`
- Deterministic:
  - `deterministic/valid_registry.csv`
  - `deterministic/holdout_registry.csv`
  - `deterministic/champion.json`
- ML:
  - `ml/candidates/{entry_candidate_labels.parquet,meta.json}`
  - `ml/experiments/experiment_registry.csv`
  - `ml/replay_valid/evaluation_registry.csv`
  - `ml/replay_holdout/evaluation_registry.csv`
  - `ml/champions/champion_registry.json`

## 9. Related Docs

- [OPEN_SEARCH_REBASELINE_RUNBOOK.md](OPEN_SEARCH_REBASELINE_RUNBOOK.md)
- [strategy_catalog.md](strategy_catalog.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)

# BankNifty System Source Of Truth

As-of date: `2026-03-08`  
Scope: active operating contract only. Historical planning notes and remediation narratives are intentionally excluded from active docs.

If any active doc conflicts with code, code wins. If active docs conflict with each other, this file wins.

## 1. Production Mode

- Live trading runtime is deterministic-first via `strategy_app.main` (`--engine deterministic`).
- `ml_pipeline_2` is the only supported ML training / publish / `ml_pure` runtime artifact source.
- Runtime ML is an entry gate/ranker overlay (`strategy_app.engines.ml_entry_policy.MLEntryPolicy`), not a standalone execution engine.
- Runtime ML is blocked unless rollout guard passes:
  - `--rollout-stage capped_live`
  - `--position-size-multiplier <= 0.25`
  - guard artifact approval (`--ml-runtime-guard-file` or `ML_ENTRY_RUNTIME_GUARD_FILE`)
- Event bus is Redis, persistence is MongoDB.
- Canonical snapshot producer is `snapshot_app.market_snapshot.build_market_snapshot()` with `version=2.0`.

## 2. Active Gating Policy

### Window readiness (formal research runs)

Formal deterministic/ML runs are allowed only when manifest validation passes:

- `all_days_v2=true`
- `schema_version=2.0`
- `trading_days>=150`

Validator path:

- `snapshot_app.historical.window_manifest.load_and_validate_window_manifest`

### Deterministic selection gates

Deterministic open-matrix selection uses baseline-relative gates in:

- `strategy_app.tools.deterministic_open_matrix`

Core constraints:

- return outperformance gate (`candidate > baseline + min_outperformance_pct`)
- drawdown not worse than configured multiple (default `1.15x` baseline DD)
- trade count floor vs baseline (default `>=70%`)
- optional strict-positive gate (`require_positive_return`)

### ML champion gates

Champion selection in:

- `strategy_app.offline_ml.entry_quality_champion_select.select_champions`

Core constraints:

- outperformance vs deterministic comparator
- optional strict-positive return gate (formal default: on)
- max drawdown gate
- min trades + trade ratio gate
- strategy concentration gate:
  - `top_strategy_return_share = abs(top_strategy_pnl) / sum(abs(strategy_pnl))`
  - threshold default `<=0.70`

## 3. Current Manifest and Split Contract

Formal cycle contract:

- one frozen `window_manifest.json` + manifest hash
- split policy from same day list: `train 60% / valid 20% / holdout 20%`
- deterministic comparator selected on valid
- ML evaluated against that comparator using identical manifest and split boundaries

Primary orchestration entrypoint:

- `strategy_app.tools.open_search_rebaseline_cycle`

## 4. Runtime ML Status Contract

- Default runtime is deterministic-only unless ML deployment inputs are explicitly provided.
- ML deployment requires both:
  - `ML_ENTRY_REGISTRY`
  - `ML_ENTRY_EXPERIMENT_ID`
- `ml_pure` deployment supports run-id switching and requires both:
  - `ML_PURE_RUN_ID`
  - `ML_PURE_MODEL_GROUP`
- In `ml_pure` run-id mode, startup is blocked unless:
  - resolved run report publish decision is `PUBLISH` or publish status is `published`
  - resolved `published_paths.model_package` exists
  - resolved `published_paths.threshold_report` exists
- Rollout safety guard is mandatory for live ML activation (`capped_live` only).
- Any rollout violation should revert to deterministic-only runtime immediately.

## 5. Promotion Criteria and Rollout

Promotion path:

1. Offline formal cycle pass (manifest-gated, strict gates)
2. Paper stage (`>=10` trading days)
3. Shadow stage (`>=10` trading days)
4. Capped live stage (`size <= 0.25`)

Capped live halt controls:

- `halt_consecutive_losses` default `3`
- `halt_daily_dd_pct` default `-0.75`

Related enforcement paths:

- `strategy_app.main`
- `strategy_eval_orchestrator.main.validate_rollout_command`

## 6. Artifact Locations (Active)

- Window manifests: `.run/window_manifest*.json`
- Open-search cycle outputs: `.run/open_search_rebaseline*/<cycle_id>/`
- Deterministic artifacts: `.../deterministic/`
- ML artifacts: `.../ml/{candidates,experiments,replay_valid,replay_holdout,champions}/`
- Live runtime logs:
  - `.run/snapshot_app/events.jsonl`
  - `.run/strategy_app/{votes.jsonl,signals.jsonl,positions.jsonl}`

## 7. Last Verified Commands

These are the canonical operator/research commands to verify current contracts:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --validate-only --base .data/ml_pipeline/parquet_data --window-manifest-out .run/window_manifest_latest.json
python -m strategy_app.tools.open_search_rebaseline_cycle --window-manifest .run/window_manifest_latest.json --parquet-base .data/ml_pipeline/parquet_data --output-root .run/open_search_rebaseline_exploratory
python -m strategy_app.tools.open_search_rebaseline_cycle --window-manifest .run/window_manifest_latest.json --formal-run --parquet-base .data/ml_pipeline/parquet_data --output-root .run/open_search_rebaseline_formal
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1
```

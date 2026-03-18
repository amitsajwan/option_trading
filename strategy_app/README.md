# strategy_app

Layer-4 strategy consumer runtime for snapshot events.

## Purpose

- Subscribes to snapshot events from Layer-3 topic.
- Calls the `StrategyEngine` contract on every snapshot.
- Classifies market regime on every snapshot before choosing which strategies run.
- Handles session lifecycle hooks:
  - `on_session_start(date)`
  - `evaluate(snapshot)`
  - `on_session_end(date)`

## Contract

Implemented in `strategy_app/contracts.py`:

- `StrategyEngine`
- `TradeSignal`

Regime/router internals:

- `strategy_app/engines/regime.py`
- `strategy_app/engines/strategy_router.py`
- `strategy_app/engines/deterministic_rule_engine.py`
- Status note: `strategy_app/IMPLEMENTATION_STATUS.md`

## Run

From repo root:

```powershell
python -m strategy_app.main --engine deterministic
```

Tune confidence gate:

```powershell
python -m strategy_app.main --engine deterministic --min-confidence 0.70
```

Enable registry-backed ML entry gating in live runtime:

```powershell
python -m strategy_app.main `
  --engine deterministic `
  --ml-entry-registry .run/canonical_eq_e2e_refreshed_rerun2/eval/evaluation_registry.csv `
  --ml-entry-experiment-id eq_core_snapshot_v1__mfe15_gt_5_v1__seg_regime_v1__lgbm_default_v1__fixed_060 `
  --ml-entry-threshold-policy fixed_custom_062
```

Switch `ml_pure` staged bundle by run-id (strict safe):

```powershell
python -m strategy_app.main `
  --engine ml_pure `
  --ml-pure-run-id 20260308_164057 `
  --ml-pure-model-group banknifty_futures/h15_tp_auto
```

This auto-resolves model/threshold artifacts from:

- `ml_pipeline_2/artifacts/published_models/<model_group>/reports/training/run_<run_id>.json`

Strict switch checks:

- `publish_decision.decision` must be `PUBLISH` or `publish_status` must be `published`
- `published_paths.model_package` must exist
- `published_paths.threshold_report` must exist

For staged releases, the resolved artifacts are:

- an atomic staged runtime bundle in `model/model.joblib`
- a staged runtime policy in `config/profiles/<profile_id>/threshold_report.json`

The live engine then runs:

1. hard deterministic prefilters
2. Stage 1 entry gate
3. Stage 2 direction choice
4. Stage 3 recipe choice

Stage 3 directly sets:

- `max_hold_bars`
- `stop_loss_pct`
- `target_pct`

If you prefer legacy explicit paths, keep using:

```powershell
python -m strategy_app.main `
  --engine ml_pure `
  --ml-pure-model-package <path-to-model.joblib> `
  --ml-pure-threshold-report <path-to-threshold_report.json>
```

Do not mix both modes in one command.

Only experiments present in the supplied registry are deployable through the live CLI.
If CLI flags are omitted, `strategy_app` will also read `ML_ENTRY_REGISTRY` and
`ML_ENTRY_EXPERIMENT_ID` from the container or shell environment.

Compose usage:

- set `STRATEGY_ML_ENTRY_REGISTRY` and `STRATEGY_ML_ENTRY_EXPERIMENT_ID` in `.env.compose`
- optional: set `STRATEGY_ML_ENTRY_THRESHOLD_POLICY` to override threshold policy without retraining
- set `STRATEGY_ENGINE=ml_pure` plus either:
  - `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP`
  - or `ML_PURE_MODEL_PACKAGE` + `ML_PURE_THRESHOLD_REPORT`
- compose injects them into the container as `ML_ENTRY_REGISTRY`, `ML_ENTRY_EXPERIMENT_ID`, `ML_ENTRY_THRESHOLD_POLICY`, and the `ML_PURE_*` runtime envs

If PowerShell interpolation is unexpectedly blank, clear stale shell vars before
starting compose:

```powershell
Remove-Item Env:ML_ENTRY_REGISTRY -ErrorAction SilentlyContinue
Remove-Item Env:ML_ENTRY_EXPERIMENT_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_ENTRY_THRESHOLD_POLICY -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_REGISTRY -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_EXPERIMENT_ID -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_THRESHOLD_POLICY -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_RUN_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_GROUP -ErrorAction SilentlyContinue
```

Risk profile (professional aggressive, controlled):

```powershell
$env:RISK_PROFILE='aggressive_safe_v1'
python -m strategy_app.main --engine deterministic
```

This profile enables tested defaults:

- `RISK_LOT_SIZING_MODE=budget_per_trade`
- `RISK_NOTIONAL_PER_TRADE=50000`
- `RISK_MAX_DAILY_LOSS_PCT=0.02`
- `RISK_MAX_CONSECUTIVE_LOSSES=3`
- `RISK_MAX_LOTS_PER_TRADE=20`

Any explicit `RISK_*` environment variable still overrides profile defaults.

Use historical replay topic:

```powershell
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1:historical
```

Consume only first 100 events:

```powershell
python -m strategy_app.main --engine deterministic --max-events 100
```

## Regime Chain

- `AVOID`: VIX spike or pre-close. No new entries.
- `HIGH_VOL`: elevated realized vol plus elevated VIX. No new entries.
- `EXPIRY`: expiry-day routing, currently conservative and primarily filter + VWAP based.
- `PRE_EXPIRY`: conservative ORB + OI routing one day before expiry.
- `TRENDING`: ORB, EMA alignment, OI buildup, previous-day level breakout.
- `SIDEWAYS`: VWAP reclaim/rejection and OI buildup.

The deterministic engine logs regime metadata on every vote and signal so Mongo/backtests can slice results by regime.

## Engine-Aware Event Metadata

Vote/signal records now include additive engine-aware fields for replay comparability and monitoring:

- `engine_mode`: `deterministic|ml|ml_pure`
- `decision_mode`: `rule_vote|ml_gate|ml_dual|ml_staged`
- `decision_reason_code`: normalized decision code (`below_threshold`, `low_edge_conflict`, `feature_stale`, etc.)
- `decision_metrics`: optional metrics payload (`ce_prob`, `pe_prob`, thresholds, edge, confidence)
- `strategy_family_version`: `DET_V1|ML_GATE_V1|ML_PURE_DUAL_V1|ML_PURE_STAGED_V1`
- `strategy_profile_id`: versioned strategy set identifier (default deterministic profile: `det_core_v1`)

For non-default deterministic router configurations, set `strategy_profile_id` in run metadata (or `--strategy-profile-id`) so comparisons remain lane/profile-consistent.

## Modularization Notes (v2.3 Phase-1)

The logging and annotation paths are now split into focused modules with `SignalLogger` kept as the public entrypoint:

- `strategy_app/logging/decision_field_resolver.py`
- `strategy_app/logging/jsonl_sink.py`
- `strategy_app/logging/redis_event_publisher.py`
- `strategy_app/logging/signal_logger.py` (orchestrator/facade)

Engine metadata annotation is centralized in:

- `strategy_app/engines/decision_annotation.py`

Both deterministic and `ml_pure` engines call this shared annotation layer so decision contract fields remain consistent across lanes.

For end-to-end replay and evaluation documentation, see:

- [../docs/strategy_eval_architecture.md](../docs/strategy_eval_architecture.md)
- [../docs/strategy_catalog.md](../docs/strategy_catalog.md)
- [../docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md](../docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md)
- [../docs/DOCS_CODE_MAP.md](../docs/DOCS_CODE_MAP.md)

## Container/Compose

- Build image:

```powershell
docker build -f strategy_app/Dockerfile -t strategy_app:local .
```

- Health command:

```powershell
python -m strategy_app.health
```

- Compose command uses:

```powershell
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1 --min-confidence 0.65
```

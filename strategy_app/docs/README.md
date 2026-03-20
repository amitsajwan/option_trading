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

## Current Status

- `deterministic` is the replay and research lane.
- `ml_pure` is the supported live lane.
- Legacy transitional runtime wrapper is removed from the CLI and runtime factory.
- Default `EXPIRY` routing is `IV_FILTER + VWAP_RECLAIM`; `EXPIRY_MAX_PAIN` is not enabled by default.
- Deterministic exits are owner-first, with helper and high-confidence non-owner fallback.

Current code-verified status for this package lives at `strategy_app/docs/CURRENT_TREE_VALIDATION.md`.

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

Supported lanes:

- `--engine deterministic`: research and replay only
- `--engine ml_pure`: live production lane

Tune confidence gate for deterministic replay:

```powershell
python -m strategy_app.main --engine deterministic --min-confidence 0.70
```

Run `ml_pure` staged bundle by run-id (strict safe):

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

If you prefer explicit paths, keep using:

```powershell
python -m strategy_app.main `
  --engine ml_pure `
  --ml-pure-model-package <path-to-model.joblib> `
  --ml-pure-threshold-report <path-to-threshold_report.json>
```

Do not mix both modes in one command.

Compose usage:

- set `STRATEGY_ENGINE=ml_pure` in `.env.compose`
- set either:
  - `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP`
  - or `ML_PURE_MODEL_PACKAGE` + `ML_PURE_THRESHOLD_REPORT`

If PowerShell interpolation is unexpectedly blank, clear stale shell vars before
starting compose:

```powershell
Remove-Item Env:ML_PURE_RUN_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_GROUP -ErrorAction SilentlyContinue
```

Risk profile (professional aggressive, controlled) for deterministic replay:

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
- `HIGH_VOL`: elevated realized vol plus elevated VIX. Routes to `IV_FILTER + HIGH_VOL_ORB`.
- `EXPIRY`: expiry-day routing, currently `IV_FILTER + VWAP_RECLAIM`.
- `PRE_EXPIRY`: conservative ORB + OI routing one day before expiry.
- `TRENDING`: ORB, EMA alignment, OI buildup, previous-day level breakout.
- `SIDEWAYS`: VWAP reclaim/rejection and OI buildup.

The deterministic engine logs regime metadata on every vote and signal so Mongo/backtests can slice results by regime.

## Deterministic Exit Priority

When a position is open, the deterministic engine evaluates hard exits before strategy exits. Strategy exits then resolve in this order:

1. owner strategy exit
2. configured helper exit
3. high-confidence non-owner exit

The default universal exit candidate set is `ORB`, `EMA_CROSSOVER`, `VWAP_RECLAIM`, and `OI_BUILDUP`, but selection is not "first exit wins" anymore.

## Engine-Aware Event Metadata

Vote/signal records now include additive engine-aware fields for replay comparability and monitoring:

- `engine_mode`: `deterministic|ml_pure`
- `decision_mode`: `rule_vote|ml_staged`
- `decision_reason_code`: normalized decision code (`below_threshold`, `low_edge_conflict`, `feature_stale`, etc.)
- `decision_metrics`: optional metrics payload (`ce_prob`, `pe_prob`, thresholds, edge, confidence)
- `strategy_family_version`: `DET_V1|ML_PURE_STAGED_V1`
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

- [STRATEGY_ML_FLOW.md](STRATEGY_ML_FLOW.md)
- [strategy_catalog.md](strategy_catalog.md)
- [CURRENT_TREE_VALIDATION.md](CURRENT_TREE_VALIDATION.md)
- [ENGINE_CONSOLIDATION_PLAN.md](ENGINE_CONSOLIDATION_PLAN.md)
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

- Compose command resolves:

```powershell
python -m strategy_app.main --engine ${STRATEGY_ENGINE} --topic market:snapshot:v1 --min-confidence 0.65
```

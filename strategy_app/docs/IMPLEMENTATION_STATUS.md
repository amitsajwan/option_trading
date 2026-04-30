# Implementation Status

As-of: `2026-04-27`

## Implemented

**Deterministic engine (`deterministic`)**

- Regime classification and regime-based strategy routing
- Position tracking with owner-first exit priority
- Portfolio risk controls (VIX halt, daily loss, consecutive loss streak, lot sizing)
- Vote, signal, and position JSONL logging
- Redis publishing for: strategy votes, trade signals, position lifecycle events
- Mongo persistence for: `strategy_votes`, `trade_signals`, `strategy_positions`
- Regime-aware telemetry fields (`regime`, `regime_conf`) for backtest slicing
- Velocity-enhanced regime classifier and entry policy (env opt-in: `STRATEGY_ENHANCED_VELOCITY`)

**ml_pure engine (`ml_pure`)**

- 3-stage inference: Stage 1 entry gate, Stage 2 direction, Stage 3 recipe selection
- Prefilter gate chain (risk halt, freshness, regime, feature completeness, liquidity)
- Stage 3 recipe sets `stop_loss_pct`, `target_pct`, `max_hold_bars` per trade
- Staged runtime bundle format (`STAGED_RUNTIME_BUNDLE_KIND`) with V1 and V2 feature view support
- `bypass_deterministic_gates` mode for research
- Post-stop cooldown bars, underlying stop/target overrides, trailing control

**GCS artifact loading (`strategy_app/utils/gcs_artifact.py`)**

- `resolve_artifact_path()` — transparent `gs://` → local cache resolution
- `download_gcs_file()` — single-object download with content-addressed cache
- `GCS_ARTIFACT_CACHE_DIR` env var controls cache root (default `~/.cache/option_trading_models/`)
- `load_staged_model_package()` and `load_staged_policy()` both call `resolve_artifact_path` internally
- Both `ML_PURE_MODEL_PACKAGE` and `ML_PURE_THRESHOLD_REPORT` accept `gs://` URLs

**Runtime infra**

- Compose and local launcher wiring
- Health checks for `strategy_app` and `strategy_persistence_app`
- Engine-aware decision annotation (`engine_mode`, `decision_mode`, `decision_reason_code`, `decision_metrics`, `strategy_family_version`, `strategy_profile_id`)
- Position lifecycle linkage: `signal_id`, `snapshot_id`, `entry_snapshot_id` on all rows
- Modular logging: `decision_field_resolver`, `jsonl_sink`, `redis_event_publisher`, `signal_logger`
- `RuntimeArtifactStore` writes `runtime_config.json`, `runtime_state.json`, and per-session metric JSONL

## Open Items

- Add tests for:
  - Regime classification
  - Strategy router selection
  - Deterministic engine regime gating
  - ml_pure staged inference gate chain
  - Mongo strategy event persistence
- Add richer evaluation analytics:
  - Per-day equity curve
  - Drawdown and streak summaries
  - CSV/Parquet export
- No live feedback loop: live outcomes do not trigger automatic retraining of `ml_pipeline_2`
- No runtime feature parity check: live rolling feature state and offline Parquet features are computed by separate codepaths

## Current Published Model

`staged_simple_s2_v1` at:

```
gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/
```

- Model package: `.../model/model.joblib`
- Threshold report: `.../config/profiles/ml_pure_staged_v1/threshold_report.json`

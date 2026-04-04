# Strategy App Detailed Design (External Review Edition)

## 1) Purpose and Scope

`strategy_app` is the strategy decisioning layer in the option-trading stack. It consumes normalized market snapshots, evaluates exactly one active strategy engine per process, performs single-position risk-aware decisioning, and emits auditable trade and position artifacts.

This document is the primary design reference for `strategy_app` and should be treated as the source of truth for routing, state transitions, and runtime boundaries.

## 2) Entry Point and Runtime Topology

### 2.1 Runtime launch

- `python -m strategy_app.main` is the primary command.
- `main.py` parses CLI arguments and environment variables.
- `build_engine()` chooses one of:
  - deterministic runtime (`--engine deterministic`)
  - pure-ml runtime (`--engine ml_pure`) via `PureMLEngine`
- `RedisSnapshotConsumer` is constructed with:
  - subscribe topic
  - optional session-lock key
  - optional max-events limit
  - optional dedupe TTL config
- Consumer starts and invokes engine callbacks.

### 2.2 Control and event paths

- `runtime/redis_snapshot_consumer.py`: receives Layer-3 snapshots from Redis.
- `contracts.py`: defines the strategy-facing contract (`StrategyEngine`, `TradeSignal`, `StrategyVote`) and shared enums.
- `health.py`: process and dependency checks (main process + Redis).

## 3) Core Design Invariants

- One process handles one stream and one engine instance.
- Each engine instance maintains at most one live position.
- One snapshot input produces zero-or-one candidate signals per cycle.
- Session boundaries are explicitly handled (`on_session_start`, `on_session_end`).
- Signals and votes are always logged and published; logger paths are not optional for auditability.
- Strategy and risk decisions are deterministic for a fixed snapshot + config tuple.

## 4) File Ownership and Linkage Map

Primary runtime modules and ownership:

- `strategy_app/main.py`
  - CLI parsing and process bootstrap
  - config resolution (env + run context)
  - engine factory and consumer wiring
- `strategy_app/runtime/redis_snapshot_consumer.py`
  - Redis pub/sub subscription
  - dedupe of `(run_id, snapshot_id)` with TTL
  - session open/close lifecycle detection
  - propagation of per-snapshot `run_context` metadata
- `strategy_app/contracts.py`
  - external snapshot/strategy contracts and canonical enums
- `strategy_app/engines/deterministic_rule_engine.py`
  - deterministic decision loop orchestration
- `strategy_app/engines/pure_ml_engine.py`
  - standalone pure-ML runtime
- `strategy_app/engines/pure_ml_staged_runtime.py`
  - staged pure-ML bundle loading, policy validation, and inference path
- `strategy_app/engines/regime.py`
  - regime classification and optional model fallback
- `strategy_app/engines/strategy_router.py`
  - regime-aware strategy registry and candidate selection
- `strategy_app/engines/snapshot_accessor.py`
  - typed accessors and derived fields over snapshots
- `strategy_app/engines/rolling_feature_state.py`
  - online rolling feature state for pure-ML
- `strategy_app/engines/decision_annotation.py`
  - consistent annotation for votes and signals
- `strategy_app/engines/strategies/all_strategies.py`
  - strategy implementations
- `strategy_app/position/tracker.py`
  - single-position state and lifecycle transitions
- `strategy_app/risk/config.py`
  - risk profile defaults and helpers
- `strategy_app/risk/manager.py`
  - lot sizing, drawdown, and kill-switch logic
- `strategy_app/logging/signal_logger.py`
  - event orchestration for persistence and publish
- `strategy_app/logging/decision_field_resolver.py`
  - normalized decision metadata fields
- `strategy_app/logging/jsonl_sink.py`
  - append-only `.jsonl` sink for run artifacts
- `strategy_app/logging/redis_event_publisher.py`
  - Redis publication of decision events
- `strategy_app/health.py`
  - health command and dependency checks

## 5) Snapshot Intake and Session Semantics

1. Consumer receives a raw payload from Redis.
2. Payload is converted to snapshot payload object.
3. Run context is merged when present.
4. Session transition is detected by snapshot date:
   - call `on_session_start` for the new session
   - close prior session via `on_session_end`
5. Dedupe gate suppresses repeated `(run_id, snapshot_id)` events within TTL.
6. Engine evaluate loop executes once for each valid snapshot.

## 6) Deterministic Runtime Flow (`--engine deterministic`)

### 6.1 Components

- Regime classifier (`regime.py`)
- Strategy router (`strategy_router.py`)
- Entry policy (`entry_policy.py`)
- Risk manager (`risk/manager.py`)
- Position tracker (`position/tracker.py`)
- Logger/publish pipeline (`logging/*`)

### 6.2 Per-snapshot decision sequence

1. `RiskManager.update(snapshot)` refreshes session risk context (PnL, drawdown windows, halts).
2. `PositionTracker.update(snapshot, ...)` evaluates exit state if position exists:
   - mark-to-market premium
   - pnl %, MFE/MAE
   - stop, target, trailing, and time-stop checks
3. `RegimeClassifier.classify(snapshot)` emits regime + confidence + metadata.
4. `StrategyRouter` picks candidate voters:
   - entries for flat state by regime
   - exits from shared/strategy-specific candidate list when in-position
5. Candidate votes are filtered through policy and risk gates.
6. Best vote is selected by configured priority and policy result.
7. `RiskManager.compute_lots` applies lot sizing on selected candidate.
8. A `TradeSignal` is emitted, or `HOLD`/no-op if no candidate passes.

### 6.3 Exit path

- Hard exits are emitted by position tracker logic: stop loss, trailing stop, target, time stop, risk halt.
- Strategy/rule-based exits can also close position depending on regime transition and candidate policy.
- Exit reason is always persisted in signal/position event payloads.

## 7) Pure ML Runtime (`--engine ml_pure`)

### 7.1 Input construction

- `SnapshotAccessor` provides raw + derived inputs.
- `RollingFeatureState` maintains online feature windows.
- Inference row is built only when freshness and completeness checks pass.

### 7.2 Runtime modes

- Legacy dual-model threshold flow.
- Staged flow:
  - prefilter gates
  - recipe selection
  - explicit `HOLD` when stage gates fail

Both modes continue to produce standard `TradeSignal` objects with risk-aware lot sizing.

## 9) Strategy and Regime Subsystem

- Regime values include `TRENDING`, `SIDEWAYS`, `HIGH_VOL`, `AVOID`, `PRE_EXPIRY`, `EXPIRY`.
- Regime output affects both candidate set and policy strictness.
- Router profile allows runtime overrides of:
  - `iv_filter_config`
  - `regime_entry_map`
  - `exit_strategies`
  - strategy profile identifiers
- Strategy catalog in `all_strategies.py`:
  - `IV_FILTER`
  - `ORB`
  - `OI_BUILDUP`
  - `EMA_CROSSOVER`
  - `VWAP_RECLAIM`
  - `EXPIRY_MAX_PAIN`
  - `PREV_DAY_LEVEL`

## 10) Risk Model and Safety State

`RiskManager` controls:

- capital-aware lot sizing
- confidence-aware downscaling within configured risk/notional caps
- max daily drawdown breaches
- consecutive-loss kill logic
- VIX/spike halt and cooldown resume

Risk context objects:

- `RiskContext` (session scope)
- `PositionContext` (open position scope)

These objects are passed into strategy evaluation and tracker updates for explicit separation and auditability.

## 11) Position Management

`PositionTracker` keeps one live state and computes lifecycle transitions:

- hold duration in snapshots
- current premium and `% pnl`
- MFE/MAE updates
- stop/target/traillng transitions
- close reasons and session accounting

## 12) Logging and Audit Pipeline

Every decision path writes structured artifacts:

- vote events (`votes.jsonl`)
- signal events (`signals.jsonl`)
- position events (`positions.jsonl`)

Position lifecycle records preserve the originating entry `signal_id` and carry snapshot linkage:

- `POSITION_OPEN`: `snapshot_id` + `entry_snapshot_id` reference the opening snapshot
- `POSITION_MANAGE`: `snapshot_id` references the current manage snapshot and `entry_snapshot_id` preserves the origin
- `POSITION_CLOSE`: `snapshot_id` references the closing snapshot and `entry_snapshot_id` preserves the origin

Canonical decision metrics and normalized contract fields are stored on the live `PositionContext` before logging so `SignalLogger` remains a serialization/publish boundary, not a runtime state mutator. The same logical events are also pushed to Redis topics.

## 13) Health and Operational Behavior

`health.py` can be used independently via `python -m strategy_app.health`.

Checks include process-level and Redis dependency status and returns explicit codes for orchestration systems.

## 14) Configuration Matrix

### 14.1 CLI

- engine selection and snapshot/topic controls
- run directory and event cap
- confidence and warmup parameters
- ML-pure specific model and threshold options

### 14.2 Environment and Profiles

- `RISK_*`, `STRATEGY_*`, `ML_*`, `ML_PURE_*` families
- `RISK_PROFILE` and runtime overrides

### 14.3 Orchestrated Runtime Context

Context provided at run time can override:

- `risk_config`
- `policy_config`
- `regime_config`
- `router_config`

This supports replay/prod parity for controlled experiments.

## 15) External Integration Notes

- `strategy_persistence_app` consumes emitted JSONL/Redis artifacts.
- Offline evaluators and research tooling replay strategy events for attribution by strategy and regime.
- The module is intentionally stateful in runtime, stateless by design in output schema to support deterministic replay.

## 16) External Review Checklist

- Validate engine factory and mode switch (`main.py`).
- Validate snapshot -> session boundary behavior (`redis_snapshot_consumer.py`).
- Validate regime assignment and routing precedence.
- Validate risk gate ordering vs. position closure priority.
- Validate strategy ownership and exit reasons.
- Validate event schema parity between JSONL and Redis channels.

## 17) Doc Governance

- Canonical docs are stored under `strategy_app/docs`.
- Root `strategy_app/*.md` files are migration stubs.
- Update this document when engine order, position ownership, or decision schema changes.
- Current snapshot-to-decision reference: `strategy_app/docs/STRATEGY_ML_FLOW.md`.
- Engine consolidation status is tracked in `strategy_app/docs/ENGINE_CONSOLIDATION_PLAN.md`.

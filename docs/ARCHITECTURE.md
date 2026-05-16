# BankNifty Architecture

This document is the current cross-cutting system view. Package-specific details live under the owning package docs.

## 1. Component Boundaries

- `ingestion_app`: live market collector and provider-authenticated data access
- `snapshot_app`: builds canonical `MarketSnapshot` (`schema_version=3.0`) and publishes snapshot events
- `strategy_app`: consumes snapshots and runs one of two engines:
  - `ml_pure` for the supported live lane
  - `deterministic` for replay and research
- `persistence_app`: persists snapshot and strategy streams to MongoDB
- `ml_pipeline_2`: offline staged training, publish gating, runtime bundle generation, and runtime handoff for `ml_pure`
- `strategy_eval_orchestrator` and UI services: optional replay and evaluation surfaces
- `market_data_dashboard`: optional operator UI

## 2. Core Contracts

### Snapshot contract

- builder: `snapshot_app.core.market_snapshot.build_market_snapshot`
- schema identity:
  - `schema_name=MarketSnapshot`
  - `schema_version=3.0`
- key blocks include:
  - `session_context`
  - `futures_bar`
  - `futures_derived`
  - `opening_range`
  - `vix_context`
  - `chain_aggregates`
  - `atm_options`
  - `iv_derived`
  - `session_levels`

### Event contract

- live topic: `market:snapshot:v1`
- historical topic: `market:snapshot:v1:historical`
- strategy topics:
  - `market:strategy:votes:v1`
  - `market:strategy:signals:v1`
  - `market:strategy:positions:v1`

## 3. Supported Runtime Lanes

The system has **two orthogonal axes**: data source (live vs replay) and strategy engine (ml_pure vs deterministic). Their combination defines the runtime lane:

| `MODE` | `ENGINE` | Lane name | Purpose |
|---|---|---|---|
| `live` | `ml_pure` | **Live (supported)** | Production trading lane with `regime_gate_v1` and `capped_live` rollout |
| `replay` | `ml_pure` | **Live-equivalence replay** | Tests live strategy behavior on historical data — same strategy code that runs live |
| `research` | `deterministic` | **Research / inspectable replay** | Deterministic rule engine for hand-traceable replay sessions |

Today, the historical-replay compose service defaults to `ENGINE=deterministic`. **That means today's replay is a test of plumbing, not of live strategy behavior.** Running replay with `STRATEGY_ENGINE=ml_pure` activates the live-equivalence lane and is the recommended mode for any pre-deployment validation.

There is no supported live runtime path where ML is layered on top of deterministic vote outputs.

## 4. Canonical Live Sequence

1. `ingestion_app` refreshes provider-backed market state.
2. `snapshot_app.main_live` builds and validates `MarketSnapshot`.
3. `snapshot_app` publishes to `market:snapshot:v1`.
4. `strategy_app.main --engine ml_pure` consumes the live topic and scores the staged runtime bundle resolved from published `ml_pipeline_2` artifacts.
5. `strategy_app` emits strategy votes, signals, and positions.
6. `persistence_app` stores snapshot and strategy streams in MongoDB.
7. Optional dashboard and UI services read Redis and MongoDB.

## 5. Historical Replay Sequence

1. `snapshot_app.historical.snapshot_batch_runner` builds canonical parquet datasets.
2. `strategy_eval_orchestrator` or replay tooling republishes those snapshots on `market:snapshot:v1:historical`.
3. `strategy_app.main --engine deterministic` consumes the historical topic.
4. Historical persistence services write isolated replay collections.
5. Evaluation surfaces reconstruct replay outputs from persisted historical data.

## 6. Historical Data And Training Sequence

1. Raw archive is normalized and built by `snapshot_app.historical.snapshot_batch_runner`.
2. Local parquet root `.data/ml_pipeline/parquet_data` contains:
  - `snapshots`
  - `snapshots_ml_flat`
  - `stage1_entry_view`
  - `stage2_direction_view`
  - `stage3_recipe_view`
3. `ml_pipeline_2.run_staged_release` trains Stage 1 / 2 / 3, scores holdout, computes `publish_assessment`, and publishes only when the staged run is eligible.
4. Live runtime switches by `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP`, or explicit bundle/report paths.

## 7. External Preconditions

Two external prerequisites commonly affect end-to-end bring-up:

1. provider credentials for `ingestion_app`
2. a valid staged `ml_pure` handoff and runtime guard for live ML

Most other containers are expected to start from repo root through Compose without special per-service setup.

## 8. Constraints

- live and historical topics must remain isolated
- live ML is allowed only in `capped_live` with a guard artifact
- deterministic remains the inspectable replay lane
- staged `ml_pipeline_2` is the only supported ML training and publish source

## 9. Storage and Persistence Contract

The system writes strategy events to two stores. They have different durability semantics and different failure modes:

### Storage roles

| Store | Role | Durability | Failure mode handling |
|---|---|---|---|
| **JSONL** (`.run/strategy_app/*.jsonl`) | **Canonical event history** for the current run. Append-only, ordered, restartable. | Per-event contract (see below) | If JSONL append fails for a critical event, container health goes red. |
| **MongoDB** (`strategy_positions*` collections) | **Derived read cache** for cross-day queries (UI, analytics). | Best-effort, eventual consistency from JSONL. | If mongo write fails, system continues. The mongo copy may lag or be incomplete; JSONL is the source of truth. |

### JSONL durability contract (per-event-type policy)

Not all events have the same durability requirements. The signal_logger chooses per event:

| Event type | JSONL policy | Why |
|---|---|---|
| `POSITION_OPEN`, `POSITION_CLOSE` | **fsync + fail-health on append error** | These are the system-of-record for trades. Loss is not acceptable. |
| `POSITION_MANAGE` | Append-only, no fsync, log on error | High volume; loss of an intermediate state is recoverable from the open/close records. |
| `decision_trace`, `vote`, `signal` | Append-only, no fsync, log on error | Debugging artifacts; loss is non-fatal for trading state. |

The implementation rule: `append_jsonl` returns `bool` (success/failure). The caller (signal_logger) decides what to do with the failure based on the event type. The sink does not know about health or policy — loose coupling.

### Mongo persistence rules

- `persistence_app` and `strategy_persistence_app` are **best-effort consumers** of redis events.
- They MUST be buffered + bulk_write to handle replay-burst load (~200 events/sec).
- They MUST recover from mongo timeouts by closing+reopening pubsub (defends against the silent-hang bug we hit 2026-05-15).
- They MUST surface health metrics: `last_message_at`, `last_flush_success_at`, `last_flush_error_at`, `buffer_depth`, `mongo_write_errors_total`, `events_dropped`.
- If mongo is unavailable, the system continues. JSONL remains the source of truth. The mongo cache backfills from JSONL on demand (future work).

### Stage 2 containment note (active 2026-05-16)

`strategy_persistence_app_historical` is currently disabled from the default historical compose profile. This is **temporary containment**, not the target architecture:

- The consumer has a documented silent-hang bug under burst load.
- JSONL captures all events reliably, so replay analysis works without it.
- Once the consumer is hardened (buffered + bulk_write + recovery + metrics), it will be re-enabled. Replay will then exercise the same persistence consumer as live, making replay a true load test for the live persistence path.

## 10. Replay Equivalence Modes

Two replay modes are conceptually distinct and should be invoked with different env:

### Live-equivalence replay (`MODE=replay ENGINE=ml_pure`)

- Same strategy code as live, same model, same gates.
- Used for: pre-deployment validation; verifying that a new ML bundle produces expected behavior on historical data; reproducing live incidents on captured snapshots.
- Result: directly comparable to what live would have done with the same input.

### Research replay (`MODE=research ENGINE=deterministic`)

- Deterministic rule engine, hand-traceable.
- Used for: inspecting cause-and-effect, debugging strategy logic, comparing rule-based baselines against ML.
- Result: NOT comparable to live ml_pure behavior. Useful for reasoning about the strategy framework, not for validating production behavior.

**Conflating these is a source of bugs.** A replay run that uses `deterministic` does not prove anything about the live `ml_pure` strategy.

## 9. Related Docs

- [SYSTEM_FLOW_DIAGRAMS.md](SYSTEM_FLOW_DIAGRAMS.md) — visual flow diagrams (training, live, replay, eval)
- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [runbooks/README.md](runbooks/README.md)
- [runbooks/GCP_DEPLOYMENT.md](runbooks/GCP_DEPLOYMENT.md)
- [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md)
- [UI_ARCHITECTURE.md](UI_ARCHITECTURE.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)
- [../strategy_app/docs/README.md](../strategy_app/docs/README.md)
- [../ml_pipeline_2/docs/README.md](../ml_pipeline_2/docs/README.md)

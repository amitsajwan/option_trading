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

- supported live lane: `strategy_app.main --engine ml_pure`
- replay and research lane: `strategy_app.main --engine deterministic`

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

## 9. Related Docs

- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [runbooks/README.md](runbooks/README.md)
- [runbooks/GCP_DEPLOYMENT.md](runbooks/GCP_DEPLOYMENT.md)
- [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md)
- [UI_ARCHITECTURE.md](UI_ARCHITECTURE.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)
- [../strategy_app/docs/README.md](../strategy_app/docs/README.md)
- [../ml_pipeline_2/docs/README.md](../ml_pipeline_2/docs/README.md)

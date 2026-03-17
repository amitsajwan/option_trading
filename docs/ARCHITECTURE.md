# BankNifty Architecture (Current)

This document describes the live architecture and active data contracts only.

## 1. Component Boundaries

- `ingestion_app`: live market collectors + market data API.
- `snapshot_app`: builds canonical `MarketSnapshot` (`version=2.0`) and publishes snapshot events.
- `strategy_app`: consumes snapshots, classifies regime, routes deterministic strategies, optionally applies ML entry gate.
- `persistence_app`: persists snapshot stream and strategy stream into MongoDB.
- `strategy_eval_orchestrator`: replays historical snapshot rows to historical topics.
- `ml_pipeline`: offline dataset/EDA/training/replay/champion selection.
- `market_data_dashboard` / `strategy_eval_ui`: operator + evaluation UI surfaces.

## 2. Core Contracts

### Snapshot contract

- Builder: `snapshot_app.market_snapshot.build_market_snapshot`
- Schema identity:
  - `schema_name=MarketSnapshot`
  - `version=2.0`
- Snapshot contains canonical blocks:
  - `session_context`, `futures_bar`, `futures_derived`, `mtf_derived`, `opening_range`
  - `vix_context`, `strikes`, `chain_aggregates`, `atm_options`, `iv_derived`, `session_levels`

### Event contract

- Envelope helper: `contracts_app.build_snapshot_event`
- Topic resolution: `contracts_app.snapshot_topic`, `contracts_app.historical_snapshot_topic`
- Primary live topic: `market:snapshot:v1`
- Primary historical topic: `market:snapshot:v1:historical`

### Strategy stream contracts

- Votes topic: `market:strategy:votes:v1`
- Signals topic: `market:strategy:signals:v1`
- Positions topic: `market:strategy:positions:v1`

## 3. Canonical Live Sequence

1. `ingestion_app` updates market API/cache from data provider.
2. `snapshot_app.main_live` reads market APIs, builds snapshot v2.0, publishes event to Redis live topic.
3. `strategy_app.main` consumes live topic and runs `DeterministicRuleEngine`.
4. `strategy_app` publishes votes/signals/positions topics.
5. `persistence_app.main_snapshot_consumer` stores snapshots in Mongo.
6. `persistence_app.main_strategy_consumer` stores strategy artifacts in Mongo.
7. Dashboard reads Redis/Mongo for operator views.

## 4. Historical Replay Sequence

1. `strategy_eval_orchestrator.main` receives replay command.
2. Reads parquet snapshots via `snapshot_app.historical.ParquetStore`.
3. Publishes replay snapshots to `market:snapshot:v1:historical`.
4. Historical strategy/persistence services consume historical topic and write historical collections.
5. Evaluation APIs reconstruct trade/equity summaries from persisted historical strategy data.

## 5. Ownership by Package

- `snapshot_app`: snapshot schema, batch/replay tooling, window readiness.
- `strategy_app`: runtime strategy decisions, regime/router/risk, ML runtime guard.
- `ml_pipeline`: offline model development and champion gating.
- `strategy_eval_orchestrator`: replay transport and rollout-stage validation.
- `persistence_app`: Mongo write path and evaluation persistence reads.

## 6. Architecture Constraints

- Live and historical flows are topic-isolated.
- Session-aware execution for live mode (`Asia/Kolkata` market window).
- Formal research runs require manifest readiness checks before execution.
- Runtime ML remains guarded and is allowed only in `capped_live` stage with approval artifact.

## 7. Related Docs

- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md)
- [SUPPORT_BRINGUP_GUIDE.md](SUPPORT_BRINGUP_GUIDE.md)
- [strategy_eval_architecture.md](strategy_eval_architecture.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)

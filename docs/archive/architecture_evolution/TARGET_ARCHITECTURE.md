# Target Architecture

> Design goal: dual-layer event bus — durable Streams for all processing, pub/sub for display-only UI notifications.

## Diagram

```
Zerodha Kite WebSocket
        ↓
  ingestion_app
        ↓  writes Redis keys + publishes pub/sub (unchanged)
        ├── market:tick:* / market:ohlc:*    (pub/sub — display only, intentionally ephemeral)
        └── options:*, depth:*              (Redis keys — REST reads)

  snapshot_app
        ↓  XADD  MAXLEN ~500
  stream:snapshots:live                      (Redis Stream — durable)
  stream:snapshots:historical                (Redis Stream — durable, per replay run)
        ↓  XREADGROUP (no ConsumerLock)
  ┌─────────────────────────────────┐
  │         strategy_app            │
  │  RedisSnapshotConsumer          │
  │  transport = streams (all modes)│
  │  NO ConsumerLock                │
  └─────────────────────────────────┘
        ↓  XADD  (already Streams ✓)
  stream:regime_decisions
        ↓
  EntryDecisionConsumer
        ↓
  stream:entry_decisions
        ↓
  DirectionDecisionConsumer
        ↓
  stream:direction_decisions
        ↓
  StrikeDecisionConsumer → RiskDecisionConsumer → ExecutionConsumer
        ↓  XADD
  stream:execution_events
        ↓
  ┌──────────────────────────────────────────────────────────┐
  │  Two separate persistence consumers (XREADGROUP each)   │
  │  persistence_app         → stream:snapshots:live        │
  │    writes: phase1_market_snapshots (Mongo)              │
  │  strategy_persistence_app → stream:execution_events     │
  │    writes: strategy_positions, votes, signals (Mongo)   │
  │    ⚠ historical variant disabled — Epic E prerequisite  │
  └──────────────────────────────────────────────────────────┘
        ↓
  MongoDB  (derived read cache — JSONL is canonical)
        ↓
  market_data_dashboard
        ├── REST reads Mongo + Redis keys (unchanged)
        ├── SSE/WebSocket reads Mongo for replay progress (not ephemeral pub/sub)
        └── WebSocket /ws — pub/sub bridge for DISPLAY channels only:
              market:tick:*, market:ohlc:*, market:strategy:signals:* (notifications)

  --- Eval Commands (durable) ---
  dashboard  →  XADD  →  stream:eval:commands
                ↓  XREADGROUP
        strategy_eval_orchestrator
                ↓  XADD progress
        stream:eval:progress:{run_id}
                ↓  dashboard reads via XREAD (last seen ID)
```

---

## Design Decisions

### 1. Streams for all processing pipelines
Every hop where a message **must not be lost** uses Redis Streams:
- snapshot delivery to strategy_app
- snapshot delivery to persistence_app
- eval run commands
- eval progress events

### 2. Pub/Sub retained for display-only channels
These channels are **intentionally ephemeral** — the browser only needs current state:
- `market:tick:{inst}:*` — live price
- `market:ohlc:{inst}:{tf}` — chart bars
- `market:strategy:signals:v1` — live signal alerts
- `indicators:{inst}:*` — technical indicator overlays

If the browser misses one tick, it gets the next one 1 second later. No durability required.

### 3. No ConsumerLock
XREADGROUP provides exclusive delivery per consumer group natively. The `ConsumerLock` Redis key (`strategy_app:consumer_lock:*`) is deleted entirely. No 120s wait on restart.

### 4. Namespace unification
`Namespace.transport()` returns `"streams"` for **all three modes** (live, oos, sim). The live/oos exception is removed. One mental model.

### 5. Dual-write shadow for cutover
During migration, `snapshot_app` dual-writes: XADD to stream AND PUBLISH to old pub/sub topic. Controlled by `SNAPSHOT_PUBSUB_SHADOW=true`. Allows `strategy_app` to be migrated independently without a coordinated deploy.

### 6. Stream sizing / retention
| Stream | MAXLEN | Rationale |
|---|---|---|
| `stream:snapshots:live` | 500 | ~1 trading day of 1-min bars |
| `stream:snapshots:historical` | 500 per run | Trimmed after replay completes |
| `stream:eval:commands` | 1000 | Commands are small; keep history |
| `stream:eval:progress:{id}` | 200 | Per-run; trimmed after run ends |
| Stage pipeline streams | 5000 | Higher for burst sim loads |

### 7. persistence_app hardening (prerequisite for re-enabling historical)
Before `strategy_persistence_app_historical` can be re-enabled:
- Buffered writes + bulk_write
- Mongo timeout recovery (close + reopen pubsub/stream connection)
- Health metrics: `last_flush_at`, `buffer_depth`, `write_errors_total`, `events_dropped`

---

## What Does NOT Change

- MongoDB role — derived read cache, not primary store
- JSONL files — remain canonical event record per run
- ML pipeline, training, parquet data
- Stage pipeline internals (regime → execution) — already correct
- Dashboard REST API surface
- Ingestion app tick/OHLC publishing to Redis keys and pub/sub display channels
- Docker Compose topology — same services, different transport config

---

## Migration Path (Zero Downtime)

```
Step 1: snapshot_app dual-write (SNAPSHOT_PUBSUB_SHADOW=true)
        → old consumers keep working, stream also populated

Step 2: strategy_app switches to streams transport
        → reads from stream, ignores pub/sub
        → ConsumerLock still in code but unused (transport=streams skips it)

Step 3: persistence_app switches to streams transport

Step 4: eval commands + progress → streams (B1, B2)

Step 5: ConsumerLock deleted from codebase (D2)
        → stream shadow flag removed (pub/sub publish removed from snapshot_app)

Step 6: Namespace.transport() returns "streams" unconditionally (D1)
```

Each step is independently deployable and reversible by env flag.

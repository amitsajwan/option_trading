# Current State — Honest Assessment

> Written: 2026-06-02. Reflects the system as it exists on `main`.

## System Map (Today)

```
Zerodha Kite WebSocket
        ↓
  ingestion_app  (port 8004)
        ↓  writes Redis keys + publishes pub/sub
        ├── market:tick:{inst}:latest          (Redis key, overwritten)
        ├── market:ohlc:{inst}:{tf}            (Redis Sorted Set)
        ├── options:{inst}:chain               (Redis key)
        └── depth:{inst}:timestamp             (Redis key)
        ↓
  snapshot_app
        ↓  PUBLISH
  market:snapshot:v1                           (pub/sub topic — LIVE)
  market:snapshot:v1:historical                (pub/sub topic — OOS/replay)
        ↓
  ┌─────────────────────────────────┐
  │         strategy_app            │
  │  RedisSnapshotConsumer          │
  │  transport = pubsub (default)   │
  │  + ConsumerLock (120s TTL)      │
  └─────────────────────────────────┘
        ↓  publishes to pub/sub
        ├── market:strategy:votes:v1
        ├── market:strategy:signals:v1
        ├── market:strategy:positions:v1
        └── market:strategy:decision_trace:v1
        ↓
  persistence_app                              (subscribes pub/sub, writes Mongo)
  strategy_persistence_app                    (subscribes pub/sub, writes Mongo)
        ↓
  MongoDB  (read cache: snapshots, signals, positions, replay runs)
        ↓
  market_data_dashboard  (port 8008)
        ├── REST API reads Redis keys + Mongo
        ├── HTTP GET → ingestion_app (live tick/depth/options)
        └── WebSocket /ws — Redis pub/sub bridge → browser
              (1 background thread per browser tab)

  --- Stage Pipeline (already Streams) ---
  stream:regime_decisions:{ns}
        ↓ XREADGROUP
  EntryDecisionConsumer → stream:entry_decisions:{ns}
        ↓
  DepthDecisionConsumer → stream:depth_decisions:{ns}
        ↓
  DirectionDecisionConsumer → stream:direction_decisions:{ns}
        ↓
  StrikeDecisionConsumer → stream:strike_decisions:{ns}
        ↓
  RiskDecisionConsumer → stream:risk_decisions:{ns}
        ↓
  ExecutionConsumer → stream:execution_events:{ns}
```

---

## What Works Well

- **Stage pipeline** (regime → execution) is already on Redis Streams — durable, ack-based, no lock needed
- **`Namespace`** abstraction cleanly separates live / oos / sim naming — single source of truth
- **`EventBus` / `RedisEventBus`** provides transport-agnostic interface — easy to switch pub/sub ↔ stream
- **JSONL** is the canonical event record — Mongo is derived, so Mongo failures are non-fatal
- **`sim` mode** already uses streams end-to-end and works correctly — proof the target architecture works

## What Is Broken / Problematic

### 1. ConsumerLock — 120s restart blackout
- File: `strategy_app/runtime/consumer_lock.py` (373 lines)
- On container restart, new `strategy_app` waits up to 125s for stale lock to expire
- Reclaim logic based on `instance_id` mitigates but does not eliminate the problem
- Root cause: pub/sub has no exclusive delivery, so we bolt on a distributed lock
- **This entire mechanism becomes unnecessary with Streams**

### 2. Snapshot pub/sub = silent message loss
- `market:snapshot:v1` and `market:snapshot:v1:historical` are fire-and-forget
- If `strategy_app` is restarting at the moment a snapshot arrives, it is permanently gone
- No replay, no re-delivery, no audit trail of what was processed
- Affects both live (rare but real) and OOS replay (can cause incomplete replay results)

### 3. Eval run commands on pub/sub
- `StrategyEvaluationService.queue_replay_run` does `PUBLISH strategy:eval:run:{id}` once
- If `strategy_eval_orchestrator` is restarting at that moment, the command is lost
- Run stays `queued` forever in Mongo with no error surfaced to the user

### 4. Dashboard WebSocket — 1 thread per browser tab
- `websocket_stomp` handler in `app.py` (line ~3565) spawns a background thread per connection
- Thread runs `pubsub.get_message(timeout=1.0)` in a tight loop
- 10 browser tabs → 10 Redis pub/sub connections + 10 threads
- No backpressure — slow browser tab blocks its own thread but otherwise uncontrolled

### 5. Live/OOS uses different transport from sim
- `Namespace.transport()` returns `"streams"` only for `sim`
- Live and OOS use pub/sub — inconsistent with the stage pipeline
- Makes reasoning about the system harder: two mental models for the same logical flow

### 6. `strategy_persistence_app_historical` disabled
- Silent-hang bug under burst load (documented in `ARCHITECTURE.md` §9)
- Disabled as containment — replay analysis works from JSONL but Mongo not populated
- Needs buffering + bulk_write + recovery before re-enabling

---

## Transport Summary

| Channel / Stream | Transport | Durable | Ack | Lock needed | Consumer |
|---|---|---|---|---|---|
| `market:snapshot:v1` | pub/sub | ❌ | ❌ | ✅ ConsumerLock | `strategy_app`, `persistence_app` |
| `market:snapshot:v1:historical` | pub/sub | ❌ | ❌ | ✅ ConsumerLock | `strategy_app_historical` |
| `stream:*:sim:{run_id}` (stage pipeline) | Redis Streams | ✅ | ✅ | ❌ | stage consumers |
| `market:strategy:*` (signals/votes/positions) | pub/sub | ❌ | ❌ | ❌ | `strategy_persistence_app` |
| `strategy:eval:run:{id}` (eval commands) | pub/sub | ❌ | ❌ | ❌ | `strategy_eval_orchestrator` |
| `market:ohlc:*`, `market:tick:*` (display) | pub/sub | ❌ | ❌ | ❌ (intentional) | WS bridge → browser |

> `strategy_persistence_app_historical` is **disabled** (silent-hang bug, Epic E). `strategy_persistence_app` (live) is active but on pub/sub — migrates in Sprint 4 / Epic E.

---

## Key Files Referenced

- `strategy_app/runtime/redis_snapshot_consumer.py` — snapshot consumer, both paths implemented
- `strategy_app/runtime/consumer_lock.py` — the lock we want to delete
- `strategy_app/runtime/stage_bus.py` — StageBus wrapping EventBus for stage pipeline
- `contracts_app/event_bus.py` — RedisEventBus: pub/sub if name doesn't start with `stream:`, Streams otherwise
- `contracts_app/sim_namespace.py` — Namespace: `transport()` returns `"streams"` for sim only today
- `contracts_app/topics.py` — pub/sub topic name functions
- `market_data_dashboard/services/strategy_evaluation_service.py` — eval run command publish
- `market_data_dashboard/app.py:3431` — WebSocket /ws STOMP bridge, thread-per-connection

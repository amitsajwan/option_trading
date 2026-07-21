# Implementation Plan — Streams & Loose Coupling

> Branch: `arch/streams-loose-coupling` (created, not yet active)
> Status: **PLANNING** — no code changed

## Epics Overview

| Epic | Title | Value | Sprint |
|---|---|---|---|
| A | Replace pub/sub snapshot delivery with Redis Streams | Eliminates ConsumerLock + message loss | 1 |
| B | Replace eval run command pub/sub with Streams | No lost replay commands on restart | 2 |
| C | Dashboard WebSocket bridge hardening | Thread safety, backpressure | 3 |
| D | Namespace unification + ConsumerLock deletion | Clean codebase, one mental model | 4 |
| E | persistence_app hardening (historical re-enable) | Replay Mongo persistence reliable | 4 |

---

## Epic A — Snapshot Delivery via Redis Streams

### A1 — snapshot_app: dual-write to stream + pub/sub shadow

**Why:** Moves the publish side to Streams while keeping backward compatibility.
Old consumers (strategy_app, persistence_app on pub/sub) continue working until they are migrated.

**Files to change:**
- `snapshot_app/redis_publisher.py` — `RedisEventPublisher.publish()`: add XADD alongside existing PUBLISH
- `snapshot_app/main_live.py` — `run_loop()`: publisher is injected via `EventPublisher` interface, no change needed here
- `snapshot_app/historical/replay_runner.py` — same pattern for OOS replay
- Add `SNAPSHOT_PUBSUB_SHADOW` env flag (default `true` during migration, `false` after D2)

**Rollback:** Set `SNAPSHOT_PUBSUB_SHADOW=true` (default). Stream write is additive — removing it just means consumers fall back to pub/sub.

**Acceptance Criteria:**
- [ ] `snapshot_app` XADDs to `stream:snapshots:live` on every snapshot publish
- [ ] `SNAPSHOT_PUBSUB_SHADOW=true` (default): also PUBLISHes to old topic (backward compat)
- [ ] `SNAPSHOT_PUBSUB_SHADOW=false`: only XADD, no pub/sub publish
- [ ] Stream MAXLEN=500 configured
- [ ] `snapshot_app` health endpoint reports `stream_depth` > 0 during session
- [ ] Existing pub/sub consumers unaffected when shadow=true

---

### A2 — strategy_app: consume from stream, remove ConsumerLock dependency

**Why:** strategy_app is the primary snapshot consumer. Switching it to Streams eliminates
the 120s restart blackout and message loss on restart. The code path already exists
(`_start_streams` in `redis_snapshot_consumer.py`) — just needs to be activated for live/oos.

**Files to change:**
- `strategy_app/runtime/redis_snapshot_consumer.py` — change `env_transport` default from `pubsub` to `streams`; wire stream name from env
- `STRATEGY_CONSUMER_TRANSPORT` env: set to `streams` in Compose files
- `STRATEGY_STREAM_NAME` env: set to `stream:snapshots:live` (live) / `stream:snapshots:historical` (oos)

> **Note:** `Namespace.transport()` in `contracts_app/sim_namespace.py` is NOT changed in A2. That unification happens in D1 only, after A1+A2+A3 are stable in production.

**Rollback:** Set `STRATEGY_CONSUMER_TRANSPORT=pubsub` and restart `strategy_app`. Shadow pub/sub still runs (A1 shadow=true), so no message loss.

**Acceptance Criteria:**
- [ ] `strategy_app` starts without acquiring any Redis lock
- [ ] `STRATEGY_CONSUMER_TRANSPORT=streams` is the default
- [ ] On `docker restart strategy_app` mid-session: pending snapshots re-delivered from PEL, zero loss
- [ ] Restart-to-first-snapshot latency < 3s (no 120s lock wait)
- [ ] Snapshot deduplication still works (snapshot_id based, existing logic unchanged)
- [ ] `test_redis_snapshot_consumer_*` tests all pass on streams transport
- [ ] `ConsumerLock.acquire()` is never called when transport=streams

---

### A3 — persistence_app: consume snapshots from stream

**Why:** Completes the snapshot stream migration. persistence_app currently subscribes to
`market:snapshot:v1` pub/sub. After A1+A2, pub/sub can be removed (A1 shadow=false).

**Files to change:**
- `persistence_app/main_snapshot_consumer.py` — switch from pubsub.subscribe to XREADGROUP on `stream:snapshots:live`
- Consumer group: `persistence-snapshots-grp-1`

**Acceptance Criteria:**
- [ ] Mongo `phase1_market_snapshots` collection populated correctly after migration
- [ ] Restart of `persistence_app` mid-replay causes zero snapshot loss (PEL re-delivery)
- [ ] No pub/sub subscription to `market:snapshot:v1` remains in persistence_app

**Rollback:** Set consumer back to pub/sub subscribe. Requires A1 shadow=true to still be active.

---

## Epic B — Eval Run Commands via Redis Streams

### B1 — Eval run command stream

**Why:** Dashboard publishes a replay run command exactly once via pub/sub. If orchestrator
is restarting, command is lost and run stays `queued` forever.

**Files to change:**
- `market_data_dashboard/services/strategy_evaluation_service.py` — `queue_replay_run()`: XADD `stream:eval:commands` instead of PUBLISH
- `strategy_eval_orchestrator/main.py` — switch from pub/sub subscribe to XREADGROUP on `stream:eval:commands`
- Consumer group: `eval-orchestrator-grp-1`

**Acceptance Criteria:**
- [ ] If orchestrator is down when command is submitted, it processes the command on next start
- [ ] Run status transitions correctly: `queued` → `running` → `done/error`
- [ ] No run stays stuck in `queued` state due to a missed pub/sub message
- [ ] Command stream MAXLEN=1000

**Rollback:** Revert `queue_replay_run()` to PUBLISH and orchestrator to pub/sub subscribe.

---

### B2 — Eval run progress events stream

**Why:** Progress updates from orchestrator to dashboard are pub/sub — if browser tab
refreshes mid-run, progress is lost and the page shows stale state.

**Files to change:**
- `strategy_eval_orchestrator/main.py` — publish progress to `stream:eval:progress:{run_id}` (XADD) in addition to pub/sub (shadow)
- `market_data_dashboard/` — progress polling reads from stream (XREAD with last-seen ID) not just WS pub/sub

**Acceptance Criteria:**
- [ ] Browser page refresh mid-run shows correct current progress
- [ ] Progress stream MAXLEN=200 per run
- [ ] Stream trimmed/deleted after run ends (no unbounded growth)
- [ ] Old WS `/topic/strategy/eval/run/{id}` bridge kept behind `EVAL_PUBSUB_SHADOW` flag for one sprint

**Rollback:** Set `EVAL_PUBSUB_SHADOW=true` to re-enable pub/sub progress path.

---

## Epic C — Dashboard WebSocket Bridge Hardening

### C1 — Shared Redis reader pool (replace thread-per-tab)

**Why:** Each browser tab spawns a background thread doing blocking Redis pub/sub.
10 tabs = 10 threads. No backpressure, no isolation.

**Files to change:**
- `market_data_dashboard/app.py` — `websocket_stomp` handler
- Replace per-connection `threading.Thread` with a shared pool (max 4 threads)
  - Rationale: Redis pub/sub is single-threaded per connection. 4 connections cover all subscribed display channels. Adding more threads adds Redis connections without throughput benefit. 4 is generous for our channel count (~6 display channel types).
- Each connection registers channel interest in a shared registry
- Pool threads dispatch to per-connection asyncio queues
- `asyncio.Queue(maxsize=100)` per connection — drop oldest on overflow (browser is display-only, misses 1 tick, gets next)

**Acceptance Criteria:**
- [ ] 10 simultaneous browser tabs → max 4 Redis threads (not 10)
- [ ] Slow/disconnected browser tab does not affect other tabs
- [ ] On tab close, connection cleanly deregisters from pool
- [ ] No deadlock under concurrent subscribe/unsubscribe

**Rollback:** Revert to per-connection thread model (previous code). No data loss risk — display only.

---

### C2 — Document and enforce pub/sub display channels as intentional

**Why:** Prevent future developers from accidentally migrating tick/OHLC to streams.
These channels are intentionally ephemeral — durability would create unbounded growth.

**Files to change:**
- `docs/architecture_evolution/TARGET_ARCHITECTURE.md` (already documented)
- `contracts_app/topics.py` — add docstring marker: `# DISPLAY_ONLY: intentionally pub/sub, not streams`
- `market_data_dashboard/app.py` — `_stomp_destination_to_redis()` add comment block

**Acceptance Criteria:**
- [ ] No stream created for `market:ohlc:*` or `market:tick:*`
- [ ] Code comments clearly distinguish display channels from durable channels
- [ ] `ARCHITECTURE.md` updated to reference this design decision

---

## Epic D — Namespace Unification + ConsumerLock Deletion

### D1 — `Namespace.transport()` returns `"streams"` for all modes

**Prerequisite:** A1, A2, A3 complete and stable in production.

**Files to change:**
- `contracts_app/sim_namespace.py` — `transport()`: remove sim-only exception, return `"streams"` unconditionally

**Acceptance Criteria:**
- [ ] `Namespace("live").transport() == "streams"`
- [ ] `Namespace("oos").transport() == "streams"`
- [ ] `Namespace("sim", run_id="x").transport() == "streams"`
- [ ] No code path branches to pub/sub based on `transport()`
- [ ] All existing namespace tests pass

---

### D2 — Delete ConsumerLock

**Prerequisite:** D1 complete. No consumer using pub/sub transport.

**Files to delete / change:**
- `strategy_app/runtime/consumer_lock.py` — **delete**
- `strategy_app/runtime/redis_snapshot_consumer.py` — remove `_consumer_lock` field, `ConsumerLock` import, all lock calls
- `contracts_app/sim_namespace.py` — remove `lock_key_for()` method (or leave as `return None` with deprecation note)
- `snapshot_app/` — remove `SNAPSHOT_PUBSUB_SHADOW` flag, remove pub/sub PUBLISH call

**Acceptance Criteria:**
- [ ] `grep -r "ConsumerLock" .` returns zero results
- [ ] `grep -r "consumer_lock" .` returns zero results (except this doc)
- [ ] Redis key `strategy_app:consumer_lock:*` never written during any test or runtime
- [ ] `strategy_app` starts within 2s in all modes with no lock acquisition log lines

---

## Epic E — persistence_app Historical Re-enable

### E1 — Harden strategy_persistence_app with buffered bulk writes

**Why:** `strategy_persistence_app_historical` is disabled due to silent-hang bug under burst load.
It needs buffering + recovery before it can be re-enabled, which would make replay a true
end-to-end load test for the live persistence path.

**Files to change:**
- `persistence_app/main_strategy_stream_consumer.py` or equivalent
- Add: in-memory buffer, `bulk_write` batching, Mongo timeout recovery, health metrics

**Acceptance Criteria:**
- [ ] Handles 200 events/sec burst without hanging
- [ ] On Mongo timeout: closes + reopens connection, resumes without data loss
- [ ] Health endpoint exposes: `last_flush_at`, `buffer_depth`, `write_errors_total`, `events_dropped`
- [ ] Re-enabled in `docker-compose.yml` historical profile
- [ ] Replay run populates `strategy_positions_historical` Mongo collection correctly

---

## Sprint Sequencing

```
Sprint 1  ──────────────────────────────────────────────────
  A1  snapshot_app dual-write (shadow mode)
  A2  strategy_app → streams (no ConsumerLock)
  A3  persistence_app → streams
  ↳ Outcome: snapshot delivery fully durable, ConsumerLock unused

Sprint 2  ──────────────────────────────────────────────────
  B1  eval command stream
  B2  eval progress stream
  ↳ Outcome: replay runs never lost on restart

Sprint 3  ──────────────────────────────────────────────────
  C1  WebSocket bridge thread pooling
  C2  Display channels documented + enforced
  ↳ Outcome: dashboard scales to many tabs, architecture clearly documented

Sprint 4  ──────────────────────────────────────────────────
  D1  Namespace.transport() unified
  D2  ConsumerLock deleted
  E1  persistence_app historical hardened + re-enabled
  ↳ Outcome: clean codebase, one mental model, full historical coverage
```

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Stream MAXLEN too small — old messages trimmed before consumer reads | Low | High | Set MAXLEN conservatively (500 = 8h of 1-min bars), monitor stream depth |
| PEL grows unbounded if consumer crashes without ACKing | Medium | Medium | Add PEL monitoring to health endpoint; max `block_ms=5000` limits hang time |
| Dual-write shadow forgotten, pub/sub never removed | Low | Low | D2 story explicitly requires removal; shadow flag has no default-true in prod |
| Browser tab pool deadlock | Medium | Medium | asyncio queue per tab + timeout prevents blocking across tabs |
| GCP Redis OOM from stream retention | Low | Medium | MAXLEN + TRIM on run end, monitor `INFO memory` |

---

## Definition of Done (per story)

1. Feature works end-to-end in local Docker Compose
2. Existing tests pass (no regressions)
3. New unit/integration test covers the changed path
4. Health endpoint reflects the new transport
5. `CURRENT_STATE.md` updated to mark item as resolved
6. No pub/sub usage remains for the migrated channel

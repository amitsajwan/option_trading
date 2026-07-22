# Architecture Decision Records

> ADR format: Context → Decision → Consequences

---

## ADR-001 — Redis Streams as the durable event bus

**Date:** 2026-06-02
**Status:** Accepted

### Context
The system uses Redis pub/sub for snapshot delivery between `snapshot_app` and `strategy_app`.
Pub/sub is fire-and-forget: no persistence, no re-delivery, no audit. We bolt on a `ConsumerLock`
to prevent duplicate consumers, creating a 120s restart blackout.

Meanwhile, the stage pipeline (regime → entry → ... → execution) already uses Redis Streams
successfully in sim mode. The `EventBus` abstraction already supports both transports.

### Decision
Move all **processing pipelines** to Redis Streams. Keep pub/sub only for **display-only**
UI notification channels (ticks, OHLC bars, indicator overlays).

### Why not Kafka?
- Kafka adds operational complexity (ZooKeeper/KRaft, broker cluster, schema registry)
- We are a single-VM operation on GCP (`e2-highmem-16`)
- Redis is already in the stack; Redis Streams provides equivalent semantics for our throughput
- ~375 snapshots/day at 1-min bars — well within Redis Streams capacity
- We can revisit if we scale to multiple instruments or higher frequency

### Why not just fix ConsumerLock?
- ConsumerLock is a workaround for a fundamental limitation of pub/sub
- Fixing the lock doesn't fix silent message loss on restart
- The Streams path already exists and is tested (sim mode)
- Deleting the lock is a simplification, not an addition

### Consequences
- `consumer_lock.py` (373 lines) deleted in Sprint 4
- `Namespace.transport()` unified to `"streams"` for all modes
- Restart blackout eliminated
- New operational concern: PEL (Pending Entry List) monitoring needed

---

## ADR-002 — Pub/Sub retained for display channels

**Date:** 2026-06-02
**Status:** Accepted

### Context
All Redis channels could theoretically be migrated to Streams. Should they be?

### Decision
Keep pub/sub for **display-only** channels:
- `market:tick:{inst}:*`
- `market:ohlc:{inst}:{tf}`
- `indicators:{inst}:*`
- `market:strategy:signals:v1` (notification only, source of truth is JSONL/Mongo)

### Reasoning
1. **Browser semantics match pub/sub.** A browser only cares about the current price tick.
   If it misses one, the next one arrives in 1s. Durability adds no value here.
2. **Unbounded growth risk.** Raw ticks at 1Hz+ stored in a stream with no consumers draining
   quickly would grow without bound unless aggressively trimmed.
3. **Already correct.** The WebSocket `/ws` bridge forwarding these to browsers works fine.
   The only problem is thread-per-tab (fixed in C1), not the transport itself.

### Consequences
- `market:tick:*` and `market:ohlc:*` stay pub/sub permanently
- Code comments in `contracts_app/topics.py` mark these as `DISPLAY_ONLY`
- Future developers must not migrate these to streams without revisiting this ADR

---

## ADR-003 — Dual-write shadow for zero-downtime migration

**Date:** 2026-06-02
**Status:** Accepted

### Context
`snapshot_app`, `strategy_app`, and `persistence_app` all need to migrate to Streams,
but they cannot all be migrated in a single coordinated deploy. We need a way to
migrate them independently.

### Decision
`snapshot_app` dual-writes during migration:
- `XADD stream:snapshots:live` (new)
- `PUBLISH market:snapshot:v1` (old, when `SNAPSHOT_PUBSUB_SHADOW=true`)

This allows `strategy_app` and `persistence_app` to be migrated independently
on their own schedule. Once all consumers are on Streams, shadow is disabled (A1 → D2).

### Consequences
- Slightly higher Redis write load during migration (two writes per snapshot)
- `SNAPSHOT_PUBSUB_SHADOW` flag must be explicitly removed in Sprint 4 (not left on forever)
- Migration order: A1 (dual-write) → A2 (strategy_app) → A3 (persistence_app) → D2 (remove shadow)

---

## ADR-004 — No raw tick streams

**Date:** 2026-06-02
**Status:** Accepted

### Context
Should raw Kite WebSocket ticks be stored in a Redis Stream for durability?

### Decision
No. Raw ticks are not stored in a stream.

### Reasoning
1. **`snapshot_app` is the feature extraction boundary.** Raw ticks are transformed into
   enriched `MarketSnapshot` objects (futures_bar, chain_aggregates, iv_derived, velocity
   features, etc.). The snapshot is the correct event to persist, not the raw tick.
2. **Volume.** Kite sends ticks at ~1Hz per instrument during market hours.
   Storing them in a stream adds no analytical value — all features are already derived.
3. **Parquet is the right store for raw historical ticks.** If we need raw tick replay,
   it comes from parquet, not Redis.

### Consequences
- Redis Streams contain only enriched snapshots (post feature-extraction)
- Raw tick display to browser continues via pub/sub (intentional, see ADR-002)
- If future work requires tick-level ML features, parquet pipeline is the correct path

---

## ADR-005 — JSONL remains canonical, MongoDB remains derived

**Date:** 2026-06-02
**Status:** Accepted (carried forward from existing ARCHITECTURE.md §9)

### Context
We have two persistence stores: JSONL files and MongoDB. Which is the source of truth?

### Decision
**JSONL is the source of truth.** MongoDB is a derived read cache.

- `POSITION_OPEN` and `POSITION_CLOSE` events: fsync + fail-health on error
- All other events: best-effort append, no fsync
- If MongoDB write fails, system continues. JSONL backfills Mongo on demand (future work).

### Consequences
- MongoDB failure is non-fatal to the trading system
- Replay analysis works from JSONL even when `strategy_persistence_app_historical` is disabled
- This decision is unchanged by the Streams migration — the transport changes, the storage contract does not

### Known Gap — Mongo backfill from JSONL
When Mongo is unavailable for a period, JSONL captures all events but Mongo falls behind.
There is no automated reconciliation. This is **out of scope for the Streams migration**.
Addressed separately: `tools/jsonl_to_mongo_backfill.py` (planned, not yet built) — a one-shot CLI script
to upsert events from JSONL into Mongo by `position_id`/`signal_id`, run manually after an outage.
See `PERFORMANCE_AND_STORAGE.md` for full JSONL assessment.

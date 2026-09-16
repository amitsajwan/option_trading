# Performance & Storage Analysis

> Written: 2026-06-02. Covers performance implications of the Streams migration and an honest assessment of JSONL.

---

## Is JSONL Good?

### What JSONL is used for

`strategy_app/logging/signal_logger.py` writes **5 JSONL files** per run:

| File | Events | fsync? | Volume |
|---|---|---|---|
| `votes.jsonl` | Every strategy vote | No | ~375/day (1 per snapshot) |
| `signals.jsonl` | Entry/exit signals | No | ~5–20/day |
| `positions.jsonl` | `POSITION_OPEN`, `POSITION_CLOSE`, `POSITION_MANAGE` | **Yes (open/close only)** | ~5–20/day |
| `decision_traces.jsonl` | Per-snapshot gate trace | No | ~375/day |
| `decisions.jsonl` | Structured decision summary | No | ~375/day |

### Where it's good (keep)

**1. Durability contract is correct.**
`append_jsonl(fsync=True)` on `POSITION_OPEN`/`POSITION_CLOSE` is ~5ms on SSD — acceptable for a signal that fires at most ~10x/day. The sink is correctly decoupled from policy (returns bool, caller decides).

**2. Append-only = crash safe.**
If the process dies mid-write, the last line may be incomplete JSON. The reader should handle this (skip malformed last line) — standard JSONL practice.

**3. Zero infrastructure dependency.**
JSONL works without Redis and without MongoDB. It's the ground-truth backup if both go down.

**4. Human-readable for post-trade analysis.**
`Get-Content .run/strategy_app/signals.jsonl -Tail 5` is how operators debug. This value is real.

### Where it's a problem (address in Epic E / F)

**1. No indexing — Mongo backfill is manual.**
`CURRENT_STATE.md` notes: *"JSONL backfills Mongo on demand (future work)"* — this is never implemented. If Mongo is down for 2 hours during trading, you can backfill, but only by hand. There is no automated reconciliation job.

**Recommendation:** Add a `tools/jsonl_to_mongo_backfill.py` script (not a daemon) as part of Epic E. Simple: read JSONL, upsert Mongo by `position_id`/`signal_id`. Run manually after an outage.

**2. Cross-day/cross-run queries are impossible on JSONL.**
Want to know "all POSITION_CLOSE events where exit_reason=STOP_LOSS in March"? You can't query JSONL — you need Mongo. This is why Mongo must stay current.

**3. `snapshot_app` also writes JSONL (`main_live.py:79-85`) but without fsync.**
The `_append_jsonl` in `snapshot_app/main_live.py` is a bare `open("a")` with no fsync and no error handling beyond `if not path: return`. This is fine for snapshot audit, but if the path is wrong it silently does nothing.

**Recommendation:** Minor — add a one-time `path.parent.mkdir(parents=True, exist_ok=True)` guard. Already done in `strategy_app/logging/jsonl_sink.py` but not in `snapshot_app/main_live.py`.

### Verdict on JSONL

**JSONL is the right choice for this system.** It is:
- Simple to operate (no schema, no migration, no database)
- Append-only and crash-safe when used with fsync on critical events
- Readable and debuggable by operators
- Independent of Redis and MongoDB

The gap is not JSONL itself — it's the **missing Mongo reconciliation** when Mongo lags or fails. That's a tooling gap, not a JSONL problem.

---

## Performance Implications of Streams Migration

### Snapshot publish (A1)

| | Current (pub/sub) | After A1 (dual-write) | After D2 (streams only) |
|---|---|---|---|
| Writes per snapshot | 1 `PUBLISH` | 1 `PUBLISH` + 1 `XADD` | 1 `XADD` |
| Network round-trips | 1 | 2 | 1 |
| Redis latency | ~0.2ms | ~0.4ms | ~0.2ms |
| CPU overhead | negligible | negligible | negligible |

~375 snapshots/day. Even at 2 round-trips during migration, this is negligible on the `e2-highmem-16` GCP VM.

### Snapshot consume (A2 — strategy_app)

| | Current (pub/sub) | After A2 (streams) |
|---|---|---|
| Blocking | `get_message(timeout=1.0)` poll | `XREADGROUP block=5000ms` |
| CPU when idle | Polls every 200ms (high idle CPU) | Blocks in Redis for 5s (near-zero idle CPU) |
| On restart | Waits 125s for lock | Reads PEL immediately (~0ms) |
| Message ordering | Delivery order only (no persistence) | Guaranteed FIFO, per stream entry ID |

**The streams path is more efficient at idle.** `pubsub.get_message(timeout=1.0)` + `time.sleep(0.2)` means 5 Redis round-trips per second even when no snapshots arrive. `XREADGROUP block=5000` makes zero Redis calls when idle.

### PEL (Pending Entry List) — new operational concern

When a consumer reads a message from a stream but doesn't ACK it (e.g., crashes), the message stays in the PEL. On restart, the consumer reads `stream_id="0"` to re-deliver pending messages before reading new ones.

**Risk:** If the PEL grows unbounded (consumer keeps crashing), re-delivery on startup takes longer.

**Mitigation already in code:**
- `_start_streams` reads pending first (`stream_id="0"`), then switches to `">"` once PEL is drained
- ACK happens immediately after `evaluate()` succeeds
- If `evaluate()` raises, the message stays in PEL and re-delivers on next start — correct behavior

**New monitoring needed (add to health endpoint):**

```python
# Add to strategy_app/health.py
xpending = redis.xpending(stream_name, group_name)
# Report: pending_count, min_idle_time_ms, max_idle_time_ms
```

Alert threshold: `pending_count > 10` or `max_idle_time_ms > 60000` (message stuck for 1min+).

### Stream memory usage

```
stream:snapshots:live   MAXLEN=500
  Each entry: ~8KB (snapshot JSON) + stream overhead ~200 bytes
  Total: 500 × 8.2KB ≈ 4.1MB  ← negligible on 128GB RAM VM
```

For reference, one day of 1-min bars (09:15–15:30) = 375 entries. MAXLEN=500 gives ~1.3 days of buffer.

### WebSocket bridge (C1)

| | Current (thread-per-tab) | After C1 (pool) |
|---|---|---|
| Threads for 10 tabs | 10 | 4 |
| Redis connections | 10 | 4 |
| Memory per tab | ~2MB (thread stack) | ~50KB (asyncio queue) |
| Total memory (10 tabs) | ~20MB | ~0.5MB |

For 10 browser tabs, the difference is small on a 128GB VM. The real benefit is **isolation** — a slow tab can't block others.

---

## What to Add to Health Endpoints

As part of each story's "performant" goal, add these metrics:

### strategy_app health (A2)
```json
{
  "transport": "streams",
  "stream_name": "stream:snapshots:live",
  "consumer_group": "consumer-group-1",
  "pending_count": 0,
  "last_ack_at": "2026-06-02T09:30:00+05:30",
  "events_processed_today": 142
}
```

### snapshot_app health (A1)
```json
{
  "stream_depth": 37,
  "stream_name": "stream:snapshots:live",
  "pubsub_shadow": true,
  "last_published_at": "2026-06-02T09:30:00+05:30"
}
```

### persistence_app health (A3)
```json
{
  "transport": "streams",
  "pending_count": 0,
  "buffer_depth": 0,
  "last_flush_at": "2026-06-02T09:30:00+05:30",
  "write_errors_total": 0,
  "events_dropped": 0
}
```

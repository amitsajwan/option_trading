# Team Handoff — Sprint 1 Work Assignments

> Branch: `arch/streams-loose-coupling`
> A1 is DONE and committed. Teams 2 and 3 start now in parallel.

---

## Status

| Story | Owner | Status | Notes |
|---|---|---|---|
| A1 — snapshot_app dual-write | Lead | ✅ **DONE** | `snapshot_app/redis_publisher.py` updated, 14 tests pass |
| A2 — strategy_app streams consumer | **Team 2** | 🔲 Ready to start | Unblocked by A1 |
| A3 — persistence_app streams consumer | **Team 3** | 🔲 Ready to start | Unblocked by A1 |

---

## For Team 2 — Story A2: `strategy_app` switch to streams

### Your task in one sentence
Switch `strategy_app` to consume snapshots from `stream:snapshots:live` (Redis Stream)
instead of `market:snapshot:v1` (pub/sub). The stream now exists thanks to A1.

### Exact steps

**1. Set up**
```bash
git fetch origin
git checkout arch/streams-loose-coupling
# Confirm you are on the right branch
git branch --show-current   # should print: arch/streams-loose-coupling
```

**2. Read these files first — in this order**
```
docs/architecture_evolution/PLAN.md        # story A2 — your spec
docs/architecture_evolution/DECISIONS.md   # why we are doing this
strategy_app/runtime/redis_snapshot_consumer.py   # full file — understand _start_streams()
```

**3. What to change**

File: `strategy_app/runtime/redis_snapshot_consumer.py`

- Line ~184: `env_transport = str(os.getenv("STRATEGY_CONSUMER_TRANSPORT") or "pubsub")...`
  → change default from `"pubsub"` to `"streams"`

- Add support for `STRATEGY_STREAM_NAME` env var to override the stream name.
  When transport=streams, use `os.getenv("STRATEGY_STREAM_NAME") or "stream:snapshots:live"`
  as the stream to consume from (instead of the pubsub topic).

- Wire this into `_start_streams()` — it already reads `self._stream_name`, make sure
  `__init__` populates it from the env var when transport=streams.

**4. DO NOT touch**
- `contracts_app/sim_namespace.py` — `Namespace.transport()` stays unchanged (that is story D1, Sprint 4)
- `strategy_app/runtime/consumer_lock.py` — do not delete it yet (story D2, Sprint 4)
- Any other file outside `strategy_app/runtime/redis_snapshot_consumer.py`

**5. Tests to run — all must pass**
```bash
python -m pytest strategy_app/tests/ -q -k "snapshot_consumer"
```

**6. New test to add**

File: `strategy_app/tests/test_redis_snapshot_consumer_streams_default.py`

Write one test: `test_streams_transport_no_lock_acquired()`
- Create `RedisSnapshotConsumer` with no `transport` argument (use env default)
- Assert `consumer._transport == "streams"`
- Assert `consumer._consumer_lock` is never `acquire()`d when `_start_streams()` is called
  (mock the bus, pass `max_events=0` to exit immediately)

**7. Commit message format**
```
feat(A2): strategy_app default transport=streams, remove lock dependency
```

**8. Done criteria (from PLAN.md)**
- [ ] `strategy_app` starts without acquiring any Redis lock
- [ ] `STRATEGY_CONSUMER_TRANSPORT=streams` is the default
- [ ] Restart-to-first-snapshot latency < 3s (no 120s lock wait)
- [ ] All existing `test_redis_snapshot_consumer_*` tests pass
- [ ] `ConsumerLock.acquire()` is never called when transport=streams
- [ ] New test `test_streams_transport_no_lock_acquired` passes

---

## For Team 3 — Story A3: `persistence_app` streams consumer

### Your task in one sentence
Switch `persistence_app` snapshot consumer from subscribing to `market:snapshot:v1` pub/sub
to reading from `stream:snapshots:live` via XREADGROUP. The stream now exists thanks to A1.

### Exact steps

**1. Set up**
```bash
git fetch origin
git checkout arch/streams-loose-coupling
git branch --show-current   # should print: arch/streams-loose-coupling
```

**2. Read these files first — in this order**
```
docs/architecture_evolution/PLAN.md              # story A3 — your spec
persistence_app/tests/test_strategy_consumer.py  # existing test pattern to follow
persistence_app/main_snapshot_consumer.py        # the file you will change
contracts_app/event_bus.py                       # RedisEventBus.consume() and .acknowledge()
```

**3. What to change**

File: `persistence_app/main_snapshot_consumer.py`

Replace the `pubsub.subscribe("market:snapshot:v1")` + `pubsub.get_message()` loop with:

```python
from contracts_app.event_bus import RedisEventBus

bus = RedisEventBus()
stream = "stream:snapshots:live"
group = "persistence-snapshots-grp-1"
consumer = "persistence-consumer-1"

bus.ensure_group(stream, group)   # creates consumer group if absent, safe to call every start

# read pending first (PEL re-delivery on restart), then new messages
read_pending = True
while not stop_event.is_set():
    stream_id = "0" if read_pending else ">"
    batch = bus.consume(stream, group, consumer, count=10, block_ms=2000, stream_id=stream_id)
    if read_pending and not batch:
        read_pending = False
        continue
    for msg_id, fields in batch:
        payload = json.loads(fields.get("payload") or "{}")
        # ... existing write logic ...
        bus.acknowledge(stream, group, msg_id)
```

Consumer group name: **`persistence-snapshots-grp-1`**

**4. DO NOT touch**
- `persistence_app/main_strategy_consumer.py` — that is story E1, Sprint 4
- Any dashboard or strategy_app files

**5. Tests to run — all must pass**
```bash
python -m pytest persistence_app/tests/ -q
```

**6. New test to add**

File: `persistence_app/tests/test_snapshot_stream_consumer.py`

Write two tests:
1. `test_pending_messages_redelivered_on_restart()` — mock bus returns 2 pending messages
   on `stream_id="0"`, then empty, then stops. Assert both messages are written and acked.
2. `test_new_messages_read_after_pending_drained()` — pending empty → switches to `">"` →
   reads 1 new message → writes and acks it.

Follow the pattern in `persistence_app/tests/test_strategy_consumer.py` (mock `_writer_thread` style).

**7. Commit message format**
```
feat(A3): persistence_app snapshot consumer switches to Redis Streams XREADGROUP
```

**8. Done criteria (from PLAN.md)**
- [ ] Mongo `phase1_market_snapshots` collection populated correctly (manual verify in Compose)
- [ ] Restart mid-replay causes zero snapshot loss — PEL re-delivers pending
- [ ] No `pubsub.subscribe("market:snapshot:v1")` remains in `persistence_app`
- [ ] Both new tests pass

---

## Integration test (after both A2 and A3 are done)

Run this to verify all three teams' work together:

```bash
python -m pytest snapshot_app/tests/test_redis_publisher_stream.py strategy_app/tests/ persistence_app/tests/ -q
```

Expected: all pass, zero regressions.

---

## Key stream names (agreed contract between teams)

| Mode | Stream name | Max entries |
|---|---|---|
| Live | `stream:snapshots:live` | 500 |
| OOS / historical replay | `stream:snapshots:historical` | 500 |

Consumer group names (do not change — chosen to be stable across restarts):
- `strategy_app`: `consumer-group-1` (existing, already used in sim mode)
- `persistence_app`: `persistence-snapshots-grp-1` (new, A3)

---

## Questions / blockers

If blocked, check `docs/architecture_evolution/PLAN.md` first.
If still unclear, raise in the session — do not guess on stream names or group names.

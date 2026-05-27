# Scrum board — Sim / Replay subsystem

**Living document.** Update Status + Owner + acceptance metrics as work lands.
**Last updated:** 2026-05-27 evening (design locked, no stories started)
**Design doc:** [project_sim_replay_design_2026-05-27 in memory](../C:/Users/amits/.claude/projects/c--code-option-trading-option-trading-repo/memory/project_sim_replay_design_2026-05-27.md) — read this first if cold.

---

## Strategic context

Today's session uncovered: live data is now rich (NIFTY, depth, full chain, block-flow) but we have no way to **replay past days through the engine with different configs and watch it play out on the dashboard**. The eval API + historical containers handle batch OOS but not interactive iteration. Building a parallel `*_sim` namespace alongside `live` and `historical` solves it.

**Four principles (locked):**
1. Loosely coupled (Redis Streams + HTTP, no shared in-process state)
2. Avoid locks (ephemeral consumer container per run; no consumer lock for sim)
3. Immutable (write-once Mongo, sealed filesystem dirs, append-only status events)
4. Easy/modular (`resolve_namespace(kind)` central; no if-kind ladders elsewhere)

---

## Team roster

| Team | Strengths | Typical surface |
|------|-----------|------------------|
| **Claude** | Backend Python, schema design, careful migrations, contracts | Engine, orchestrator, schema |
| **Cursor** | Frontend JSX, ops scripts, fast iteration on visible surface | Dashboard, compose, smoke tests |

Assignment column below is a **suggestion**, not a constraint — pick what each team is best positioned for that day.

---

## Dependency graph

```
                         ┌─────────────┐
                         │ SIM-1       │  foundation
                         │ namespace + │
                         │ manifest    │
                         └──────┬──────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
       ┌──────▼──────┐   ┌─────▼─────┐   ┌──────▼──────┐
       │ SIM-2       │   │ SIM-3     │   │ SIM-7       │
       │ Mongo init  │   │ publisher │   │ read-path   │
       │ + TTL       │   │ CLI       │   │ helper +    │
       │ + kind      │   │           │   │ badges      │
       └──────┬──────┘   └─────┬─────┘   └──────┬──────┘
              │                │                │
              │         ┌──────▼──────┐         │
              │         │ SIM-4       │         │
              │         │ streams in  │         │
              │         │ consumer    │         │
              │         └──────┬──────┘         │
              │                │                │
              │         ┌──────▼──────┐         │
              │         │ SIM-5       │         │
              │         │ compose svc │         │
              │         │ template    │         │
              │         └──────┬──────┘         │
              │                │                │
              └────────┬───────┴────────────────┘
                       │
                ┌──────▼──────┐
                │ SIM-6       │
                │ orchestrator│
                │ API         │
                └──────┬──────┘
                       │
                ┌──────▼──────┐
                │ SIM-8       │
                │ LIVE tab    │
                │ sim picker  │
                └──────┬──────┘
                       │
                ┌──────▼──────┐
                │ SIM-9       │
                │ E2E smoke + │
                │ runbook     │
                └─────────────┘

SIM-10 (cleanup cron) is post-launch, no dependency on others.
```

**Parallel tracks after SIM-1 ships:**
- Track A (Claude-shaped): SIM-2 → SIM-3 → SIM-4 → SIM-5 → SIM-6
- Track B (Cursor-shaped): SIM-7 → SIM-8 (SIM-8 blocked on SIM-6 landing)

---

## Board snapshot

| ID | Story | Priority | Suggested owner | Status | Pts |
|----|-------|----------|------------------|--------|-----|
| SIM-1 | Namespace resolver + manifest contract | **P0** | Claude | **Backlog** | 3 |
| SIM-2 | Mongo schema init (`*_sim` collections + TTL + `kind` field) | **P0** | Claude | **Backlog** | 2 |
| SIM-3 | Sim publisher CLI | **P0** | Claude | **Backlog** | 3 |
| SIM-4 | Redis Streams support in `redis_snapshot_consumer.py` | **P0** | Claude | **Backlog** | 5 |
| SIM-5 | `strategy_app_sim` compose service template | **P1** | Cursor | **Backlog** | 2 |
| SIM-6 | Orchestrator API endpoints on dashboard | **P0** | Claude | **Backlog** | 5 |
| SIM-7 | Dashboard `collection_for(kind)` helper + REPLAY/EVAL badges | **P1** | Cursor | **Backlog** | 3 |
| SIM-8 | LIVE-tab "watch sim run" picker | **P1** | Cursor | **Backlog** | 3 |
| SIM-9 | End-to-end smoke test + ops runbook | **P1** | Cursor | **Backlog** | 2 |
| SIM-10 | Cleanup cron (GC old sim dirs + TTL audit) | P2 | Either | **Backlog** | 2 |

Total: 30 points. **MVP definition: SIM-1 through SIM-6 + SIM-9.** After MVP, sim is usable via curl + REPLAY tab; SIM-7/SIM-8 polish the LIVE-tab UX.

---

## How to use this board

1. Pick a story → set Owner → move to **In progress**.
2. Read the **Context** + **Interface contract** sections carefully before touching code. They name the exact field/type/path that other stories depend on. If you change one, update the design doc + flag in slack.
3. Check off **Tasks** as you go.
4. When **Acceptance criteria** all green, move to **In review**.
5. Reviewer (other team) merges → **Done**. Paste commit SHA into the row.

---

## SIM-1 · Namespace resolver + manifest contract · P0 · 3 pts

**Foundation.** Nothing else can ship without this. Once it lands, everything else can run in parallel.

**Tasks**
- [ ] New module `contracts_app/sim_namespace.py` exporting `resolve_namespace(kind: Literal["live","oos","sim"], run_id: str | None = None) -> Namespace`
- [ ] `Namespace` is a frozen dataclass with methods: `collection_for(base)`, `stream_for(what)`, `state_key_for(key)`, `run_dir_for()`, `lock_key_for()`, `transport()`
- [ ] New module `contracts_app/sim_manifest.py` defining the `SimManifest` pydantic model (fields per design doc) + `compute_config_hash(env_overrides, image_digest, speed) -> str` (sha256 hex digest)
- [ ] Tests in `tests/test_sim_namespace.py` covering ALL three kinds, asserting return values are different across kinds
- [ ] Tests in `tests/test_sim_manifest.py` covering: schema validation, deterministic config_hash, round-trip JSON
- [ ] No imports of `sim_namespace` outside this PR yet — the module is the contract, callers come later

**Interface contract** (later stories WILL import these — names are locked):
```python
from contracts_app.sim_namespace import resolve_namespace, Namespace
from contracts_app.sim_manifest import SimManifest, compute_config_hash

ns = resolve_namespace("sim", run_id="018f7a...")
ns.collection_for("phase1_market_snapshots")  # → "phase1_market_snapshots_sim"
ns.stream_for("snapshots")                     # → "stream:snapshots:sim:018f7a..."
ns.run_dir_for()                               # → Path("/app/.run/strategy_app_sim/018f7a...")
ns.lock_key_for()                              # → None (sim never locks)
ns.transport()                                 # → "streams"

resolve_namespace("live").transport()          # → "pubsub"
resolve_namespace("oos").collection_for("strategy_votes")  # → "strategy_votes_historical"
```

**Acceptance criteria**
- [ ] All tests pass: `python -m pytest tests/test_sim_namespace.py tests/test_sim_manifest.py -v`
- [ ] Mypy/pyright clean on the new modules
- [ ] Zero references from elsewhere in the codebase (this PR is foundation only)
- [ ] Reviewer can grep for `phase1_market_snapshots_sim` and confirm the only producer of that string is `resolve_namespace`

**Files touched**
- `contracts_app/sim_namespace.py` (new)
- `contracts_app/sim_manifest.py` (new)
- `tests/test_sim_namespace.py` (new)
- `tests/test_sim_manifest.py` (new)
- `contracts_app/__init__.py` (add exports)

---

## SIM-2 · Mongo schema init · P0 · 2 pts

**Depends on:** SIM-1 (uses collection names from `resolve_namespace`)

**Tasks**
- [ ] New script `ops/migrations/sim_namespace_init.py` — idempotent migration that:
  - [ ] Creates `*_sim` collections (snapshots, votes, signals, positions, decision_traces, depth_ticks) if absent
  - [ ] Adds TTL index on `created_at` field (`expireAfterSeconds=2592000`, 30d) for each `*_sim` collection
  - [ ] Adds compound index `(run_id, created_at)` for fast per-run queries
  - [ ] Adds `kind` field to existing `strategy_eval_runs` docs that lack it (default `"oos"` for safety)
  - [ ] Creates index on `strategy_eval_runs.kind` + `strategy_eval_runs.created_at`
- [ ] Reads collection names via `resolve_namespace("sim").collection_for(base)` — no hardcoded strings
- [ ] Dry-run mode: `--dry-run` prints what would be done without writing
- [ ] Logs every action with collection name + what it did (created/skipped/indexed)

**Interface contract**
- All sim collections honor schema: every doc has `{run_id: str, kind: "sim", created_at: datetime}` top-level fields. Other fields per the source schema unchanged.

**Acceptance criteria**
- [ ] Run once on VM Mongo → 6 `*_sim` collections present with TTL + run_id indexes
- [ ] Run twice in a row → second run is a no-op (idempotent)
- [ ] `db.strategy_eval_runs.find({kind: {$exists: false}})` returns 0 after migration
- [ ] Reviewer checks `db.phase1_market_snapshots_sim.getIndexes()` and sees the TTL + compound index

**Files touched**
- `ops/migrations/sim_namespace_init.py` (new)
- `ops/migrations/__init__.py` (new, may need)
- `tests/test_sim_namespace_init.py` (new — uses mongomock or skip if unavailable)

---

## SIM-3 · Sim publisher CLI · P0 · 3 pts

**Depends on:** SIM-1, SIM-2

**Tasks**
- [ ] New script `ops/sim/run_sim_publisher.py` with CLI args:
  - `--run-id` (required)
  - `--source-coll` (default `phase1_market_snapshots`)
  - `--source-date YYYY-MM-DD` (required)
  - `--speed` (float, default 30; means 60/speed seconds between bars)
  - `--label` (string, written into manifest)
  - `--max-len` (int, default 10000; Redis stream MAXLEN approximation)
- [ ] On start: write `manifest.json` to `resolve_namespace("sim", run_id).run_dir_for()`; compute `config_hash`, `git_commit` (from `git rev-parse HEAD`), `image_digest` (skip — orchestrator fills this in P4)
- [ ] Read snapshots from source coll in `_id` order, filter by `trade_date_ist == source_date`
- [ ] For each snapshot: `XADD <stream_name> MAXLEN ~ <max_len> * payload <json>` where payload has `meta.source_mode="sim", meta.run_id, meta.sim_label` injected
- [ ] Sleep `60/speed` seconds between bars (use monotonic clock — don't drift)
- [ ] At end of corpus: `XADD <stream_name> * sentinel 1 run_id <run_id>` and exit 0
- [ ] On SIGINT/SIGTERM: write sentinel with `aborted=1`, update manifest `terminal_status=cancelled`, exit
- [ ] All Redis/Mongo connection params via env vars (MONGO_URI, REDIS_HOST/PORT)
- [ ] Tests in `tests/test_run_sim_publisher.py` using `fakeredis` + `mongomock`

**Interface contract**
- Stream name MUST come from `resolve_namespace("sim", run_id).stream_for("snapshots")` — no string concat
- Sentinel event: `{type: "sentinel", run_id: <rid>, aborted: 0|1, total_published: <n>}` so consumer can stop cleanly

**Acceptance criteria**
- [ ] Smoke test on VM: publish today's 375 snapshots at speed=30, observe stream length growing in `redis-cli XLEN <stream>`
- [ ] Sentinel arrives at end; consumer (when built) terminates cleanly
- [ ] Manifest file present + readable in run dir
- [ ] Killing the publisher mid-run leaves manifest with `terminal_status=cancelled`

**Files touched**
- `ops/sim/run_sim_publisher.py` (new)
- `ops/sim/__init__.py` (new)
- `tests/test_run_sim_publisher.py` (new)

---

## SIM-4 · Redis Streams support in `redis_snapshot_consumer.py` · P0 · 5 pts

**Depends on:** SIM-1

**Tasks**
- [ ] Add env var `STRATEGY_CONSUMER_TRANSPORT` ∈ {`pubsub` (default), `streams`}
- [ ] When `streams`: use XREADGROUP loop instead of pubsub
  - [ ] Stream name from env `STRATEGY_STREAM_NAME` (orchestrator stamps this)
  - [ ] Consumer group: `"consumer-group-1"`, consumer name: `"consumer-{hostname}"`
  - [ ] Create group on startup if absent (XGROUP CREATE MKSTREAM, ignore BUSYGROUP)
  - [ ] Block for up to 5s per XREADGROUP call; loop
  - [ ] XACK each message after processing succeeds
  - [ ] On sentinel event: log "received sentinel, shutting down", graceful exit (don't XACK the sentinel; let it sit as pending so it's auditable)
- [ ] When `STRATEGY_CONSUMER_LOCK_ENABLED=false`: skip lock acquire entirely (no `_consumer_lock.acquire()` call). Add this env var; default true.
- [ ] Snapshots arriving via streams MUST have their `meta.source_mode` + `meta.run_id` propagated through `engine.set_run_context(run_id, metadata)`
- [ ] Tests in `strategy_app/tests/test_streams_consumer.py` using fakeredis:
  - [ ] Stream of 5 events + sentinel → consumer processes 5, exits on sentinel
  - [ ] Crash mid-stream → restart → resumes from pending (XREADGROUP with `>` first time, then specific IDs to redeliver — verify with test)
  - [ ] Sentinel with `aborted=1` → consumer logs cancellation but still exits gracefully

**Interface contract**
- Engine code (deterministic_rule_engine.py etc.) is **unchanged** — the consumer hides the transport difference. Engine just sees `evaluate(snapshot)` calls in order, same as pubsub.
- The `meta` dict on each event has the same shape as live's `meta` block — just with `source_mode` + `run_id` added.

**Acceptance criteria**
- [ ] Existing pubsub path still works (live + historical eval unchanged) — regression test
- [ ] Streams path works against `fakeredis` in tests
- [ ] Manual smoke: publish via SIM-3, consume via this code with `STRATEGY_CONSUMER_TRANSPORT=streams STRATEGY_STREAM_NAME=<x>`, observe each event consumed exactly once
- [ ] No `consumer_lock` errors when `STRATEGY_CONSUMER_LOCK_ENABLED=false`

**Files touched**
- `strategy_app/runtime/redis_snapshot_consumer.py` (modify)
- `strategy_app/tests/test_streams_consumer.py` (new)

---

## SIM-5 · `strategy_app_sim` compose service template · P1 · 2 pts

**Depends on:** SIM-1, SIM-4

**Tasks**
- [ ] Add `strategy_app_sim` service to `docker-compose.yml`, mirroring `strategy_app_historical` structure but with sim-mode env:
  - [ ] `STRATEGY_CONSUMER_TRANSPORT=streams`
  - [ ] `STRATEGY_CONSUMER_LOCK_ENABLED=false`
  - [ ] `STRATEGY_RUN_DIR=/app/.run/strategy_app_sim/${SIM_RUN_ID:?run_id required}`
  - [ ] `STRATEGY_STREAM_NAME=stream:snapshots:sim:${SIM_RUN_ID}`
  - [ ] `MONGO_COLL_SNAPSHOTS=phase1_market_snapshots_sim`
  - [ ] `MONGO_COLL_STRATEGY_VOTES=strategy_votes_sim` (etc., all sim variants)
  - [ ] `restart: "no"` — these are one-shot containers
  - [ ] Bind-mount the per-run filesystem dir
- [ ] Service is NOT in default `up` set — only invoked by orchestrator via `docker compose run`
- [ ] Document in `docker-compose.yml` comments: "spawned per-run by orchestrator, not by manual `up`"

**Interface contract**
- Orchestrator invokes: `docker compose --env-file .env.compose run --rm -d -e SIM_RUN_ID=<rid> strategy_app_sim`
- Container exits on sentinel; no liveness probes needed
- Container name pattern: `option_trading-strategy_app_sim-run-<short_rid>` (so multiple runs don't collide)

**Acceptance criteria**
- [ ] `docker compose config --services` shows `strategy_app_sim`
- [ ] Manual smoke: `SIM_RUN_ID=test-1 docker compose run --rm strategy_app_sim` starts container, attaches to stream, exits when sentinel arrives
- [ ] Filesystem dir `/app/.run/strategy_app_sim/test-1/` populated with expected files

**Files touched**
- `docker-compose.yml` (modify)

---

## SIM-6 · Orchestrator API · P0 · 5 pts

**Depends on:** SIM-1, SIM-2, SIM-3, SIM-4, SIM-5

**Tasks**
- [ ] New router `market_data_dashboard/routes/sim_routes.py` mounting:
  - `POST /api/sim/runs` — body: `{source_date, source_coll, label, env_overrides, speed}`. Allocates UUIDv7 run_id; writes manifest to run dir + `strategy_eval_runs`; spawns publisher subprocess; spawns consumer container; returns `{run_id, manifest_path, stream_name, dashboard_url}`
  - `GET /api/sim/runs?date=YYYY-MM-DD&limit=N` — paginated list (newest first)
  - `GET /api/sim/runs/{run_id}` — manifest + current status + summary metrics
  - `DELETE /api/sim/runs/{run_id}` — sends SIGTERM to publisher + container; writes `terminal_status=cancelled`
- [ ] Manifest writer: computes `git_commit` (subprocess), `image_digest` (docker inspect strategy_app_sim image), `config_hash` (SIM-1 helper). Writes atomic (tmpfile + rename).
- [ ] Sentinel poller: background asyncio task watching for sentinel on the stream; on sentinel: marks `terminal_status=completed`, computes summary stats from `*_sim` collections, runs `chmod -R a-w` on run dir to seal it
- [ ] All file ops gated by run-dir-exists check; never overwrite an existing run
- [ ] OpenAPI schema auto-generated from pydantic models
- [ ] Tests in `market_data_dashboard/tests/test_sim_routes.py` with mocked subprocess + docker

**Interface contract**
- `POST /api/sim/runs` is synchronous about *allocation* (returns immediately with run_id) but ASYNC about *execution* (caller polls GET to learn outcome). The 200 response means "spawn initiated successfully," not "run completed."
- `env_overrides` keys MUST be a whitelist — see allowed list in design doc; reject unknown keys with 400. Whitelist: `STRATEGY_PROFILE_ID, STRATEGY_ENGINE, ENTRY_TIME_WINDOWS, ENTRY_REGIME_ALLOWED_TAGS, ENTRY_REGIME_TAGGER, DIRECTION_ML_MODEL_PATH, DIRECTION_ML_WEIGHT, DIRECTION_ML_FILTER_MIN_PROB, ENTRY_ML_MODEL_PATH, ENTRY_ML_MIN_PROB, ML_PURE_RUN_ID, ML_PURE_MODEL_GROUP, ML_PURE_MODEL_PACKAGE, OPTION_PNL_MODEL_BUNDLE, BRAIN_ENABLED, BRAIN_CONSENSUS_MIN_AGREEING, STRATEGY_IV_EXTREME_PERCENTILE`
- All read paths in this router use `resolve_namespace("sim").collection_for(...)` exclusively

**Acceptance criteria**
- [ ] curl POST → returns run_id within 2s
- [ ] Concurrent runs (POST 3 in parallel) → 3 distinct run_ids, 3 distinct containers, 3 distinct stream names; no cross-contamination
- [ ] Killing publisher + DELETE call → status flips to cancelled within 5s
- [ ] After completion: `ls -la /app/.run/strategy_app_sim/<rid>/` shows all files have no write permission
- [ ] Whitelist enforcement: POST with `env_overrides: {RANDOM_VAR: 1}` returns 400

**Files touched**
- `market_data_dashboard/routes/sim_routes.py` (new)
- `market_data_dashboard/routes/schemas/sim.py` (new — pydantic models)
- `market_data_dashboard/app.py` (mount router)
- `market_data_dashboard/tests/test_sim_routes.py` (new)

---

## SIM-7 · Dashboard read-path helper + REPLAY/EVAL badges · P1 · 3 pts

**Depends on:** SIM-1

**Tasks**
- [ ] Python helper `market_data_dashboard/_namespace.py` (thin wrapper over `contracts_app.sim_namespace`) so dashboard code has one local import surface
- [ ] Refactor `market_data_dashboard/real_source.py` to use `_namespace.collection_for(kind)` in every Mongo `db[...]` access. No hardcoded collection-name strings remain.
- [ ] Extend `MonitorSource` to accept `kind` param (default "live"); plumb through to all read methods
- [ ] Route changes:
  - `/api/strategy/decisions?kind=sim&run_id=...&date=...`
  - `/api/strategy/blocker-funnel?kind=sim&run_id=...&date=...`
  - `/api/strategy/decisions?kind=oos&...` (existing behaviour, just made explicit)
  - Default `kind=live` if absent (backwards compat)
- [ ] Update REPLAY tab (`terminal-live.jsx`) dropdown: query `/api/sim/runs` + existing `/api/strategy/evaluation/runs`, merge into single list with badge `[OOS]` or `[SIM]`
- [ ] EVAL tab: same badge treatment in the runs table

**Interface contract**
- All dashboard read URLs gain optional `kind` query param. Missing/null → defaults to "live" for backwards compat with existing live UI.

**Acceptance criteria**
- [ ] Grep for `phase1_market_snapshots` in `market_data_dashboard/` — only result is in `_namespace.py`
- [ ] REPLAY tab dropdown shows runs from both kinds with visible badge
- [ ] EVAL tab table has a `kind` column; can filter by it
- [ ] Existing live-tab behaviour unchanged

**Files touched**
- `market_data_dashboard/_namespace.py` (new)
- `market_data_dashboard/real_source.py` (modify)
- `market_data_dashboard/routes/strategy_current_routes.py` (modify)
- `market_data_dashboard/routes/monitor_ws.py` (modify if it reads collections)
- `market_data_dashboard/static/webapp/terminal-live.jsx` (modify REPLAY dropdown)
- `market_data_dashboard/static/webapp/eval.jsx` (modify EVAL table)

---

## SIM-8 · LIVE-tab "watch sim run" picker · P1 · 3 pts

**Depends on:** SIM-6, SIM-7

**Tasks**
- [ ] Add small dropdown in top-right of LIVE tab: "watching: LIVE ▾". Options:
  - `LIVE` (default; current behaviour)
  - For each sim run today: `SIM · <label> · <run_id_short>`
- [ ] On selection of a sim run: swap the LIVE tab's underlying fetches to use `?kind=sim&run_id=<rid>` for every endpoint it calls (snapshots, votes, decisions, brain, KPIs)
- [ ] WS subscription: send `{action: "subscribe", mode: "sim", run_id: "..."}` instead of `mode: "live"`; backend monitor_ws.py routes to sim collections
- [ ] Visible "watching sim run X" banner so user can't forget they're not on live
- [ ] "Back to LIVE" button always present in the banner

**Interface contract**
- `monitor_ws.py` accepts new `mode: "sim"` with `run_id` in subscribe payload; uses `resolve_namespace("sim", run_id)` to pick collections

**Acceptance criteria**
- [ ] Switching from LIVE → SIM run swaps chart + brain badge + decisions panel + KPIs in <2s
- [ ] Switching back restores live data without page reload
- [ ] Banner is unmissable when in sim mode (color + sticky position)

**Files touched**
- `market_data_dashboard/static/webapp/terminal-live.jsx` (modify)
- `market_data_dashboard/routes/monitor_ws.py` (extend WS protocol)

---

## SIM-9 · E2E smoke test + ops runbook · P1 · 2 pts

**Depends on:** SIM-6 (everything must work for the smoke test to pass)

**Tasks**
- [ ] New script `ops/sim/smoke_test.sh` that:
  - [ ] Picks the most recent live date with ≥100 snapshots
  - [ ] POSTs `/api/sim/runs` with default config + label `smoke_test`
  - [ ] Polls until terminal_status; asserts `completed`
  - [ ] Asserts: manifest present, filesystem sealed, at least 1 doc in each `*_sim` collection tagged with the run_id
  - [ ] Cleans up: DELETE the run (will let TTL drop it eventually anyway)
  - [ ] Exits 0 on success, non-zero with diagnostic logs on failure
- [ ] New doc `docs/runbooks/SIM_REPLAY_RUNBOOK.md`:
  - [ ] How to trigger a sim run via curl
  - [ ] How to inspect a running sim (logs, stream length, container)
  - [ ] How to compare two runs in the EVAL tab
  - [ ] How to debug a stuck/cancelled run
  - [ ] How to clean up sim data manually if needed (TTL is automatic, but document the override)

**Acceptance criteria**
- [ ] `bash ops/sim/smoke_test.sh` exits 0 on a healthy VM
- [ ] Runbook covers the 5 most common operator scenarios

**Files touched**
- `ops/sim/smoke_test.sh` (new)
- `docs/runbooks/SIM_REPLAY_RUNBOOK.md` (new)

---

## SIM-10 · Cleanup cron · P2 · 2 pts

**Depends on:** SIM-1 (uses run_dir_for)

**Tasks**
- [ ] Daily cron / systemd timer: `ops/cron/sim_gc.sh`
  - [ ] Walks `/app/.run/strategy_app_sim/`; deletes dirs older than 30d
  - [ ] Verifies Mongo TTL is working (sample query asserting no docs older than 30d in `*_sim` collections)
  - [ ] Logs a summary: dirs deleted, size freed
- [ ] Hook into existing systemd timer infrastructure (TOTP timer is the precedent)

**Acceptance criteria**
- [ ] First run on a fresh VM is a no-op; doesn't fail on missing dirs
- [ ] Synthetic test: create a fake dir dated 31d ago → cron deletes it next run

**Files touched**
- `ops/cron/sim_gc.sh` (new)
- `ops/cron/sim-gc.service` + `.timer` (new systemd units)
- `docs/runbooks/SIM_REPLAY_RUNBOOK.md` (mention GC behaviour)

---

## Definition of done (applies to every story)

- [ ] All listed tests pass locally + in CI (when CI exists)
- [ ] No new hardcoded references to sim/oos/historical collection names outside `sim_namespace.py`
- [ ] PR description quotes the **Interface contract** section of the story so reviewer can verify
- [ ] Code reviewed by the OTHER team (cross-team review enforces interface clarity)
- [ ] Commit message references story ID (`SIM-3:` prefix)
- [ ] Design doc updated if the implementation diverged from the plan

---

## Cross-team coordination

- **Daily 5-min sync** (async OK): which story is each team in? Any interface tension?
- **Single source of truth for interfaces** = the design doc memory entry. Update it before changing a contract; don't surprise the other team.
- **No story is done until interface tests pass.** If SIM-1's `Namespace.collection_for` is called wrong in SIM-3, that's SIM-3's bug, not SIM-1's — but it means SIM-1's tests weren't strict enough; reviewer should push back.
- **When in doubt, ask in this board, not in DMs.** Public discussion makes the design self-documenting.

---

## Out of scope (deferred — track separately if/when needed)

- Tick + depth-tick replay alongside snapshots (only if bar-only sim turns out to be too low fidelity vs live)
- Multi-day sim runs (write a wrapper around single-day if needed)
- Online config swap (sim container is ephemeral; restart-to-change is fine)
- Sim-of-sim (replay a sim run's output as input to another sim — complicates source_coll resolution)
- GCS bundle sharing across machines (single-VM operation is fine for now)
- Parallel-run resource limits (Mongo / Redis throttling when >10 sim runs concurrent)

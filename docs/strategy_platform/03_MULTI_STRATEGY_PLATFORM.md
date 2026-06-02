# 03 — Multi-Strategy Platform (the future vision)

*Run scalper AND lottery (AND future strategies) concurrently — each in its own
container, its own config, its own book — sharing one market-data feed and one
execution layer. This is the long-term direction, designed for now so today's
decisions don't block it.*

---

## 1. The idea

Today: **one** `strategy_app`, **one** profile, **one** exit mode at a time. To compare
scalper vs lottery you sim or you switch a flag.

Future: **several strategy containers running at once on the same live data.** Scalper
takes its trades, lottery takes its trades, a third strategy (mean-reversion, expiry-day,
overnight gamma, whatever) takes its. Each is an independent "fund" with its own capital
slice and risk budget. The portfolio is the sum.

This is feasible *because of the two principles*: the snapshot feed is pub/sub fan-out
(N consumers, same data), and every strategy is fully config-driven (so each container is
the same image with a different env).

---

## 2. Why the current architecture already supports it (mostly)

```
                       market:snapshot:v1   (Redis pub/sub — fan-out)
                        ┌──────────┬──────────┬───────────┐
                        ▼          ▼          ▼            ▼
                 strategy_app  strategy_app  strategy_app  ...
                  (scalper)    (lottery)    (meanrev)
                        │          │          │
        each writes to its OWN namespaced topics + run dir + run_id:
          market:strategy:signals:v1:<strat>   .run/strategy_app_<strat>/
                        └──────────┴──────────┴───────────┐
                                                          ▼
                                                   execution_app
                                          (routes per-strategy signals,
                                           tags broker orders by strategy,
                                           tracks per-strategy fills)
                                                          │
                                                          ▼
                                            portfolio risk manager + dashboard
                                              (aggregate across strategies)
```

Already true today:
- Snapshot topic is fan-out — multiple subscribers get every snapshot.
- `STRATEGY_RUN_DIR`, topic names, `run_id`, profile, and exit mode are all env-driven.
- `docker-compose` already runs strategy variants under profiles (live/historical/sim).
- `execution_app` already routes by signal and tags Kite orders.

So a second strategy is, in principle, **another container with a different env block.**

---

## 3. What's missing (the real work)

### 3.1 Topic + run-dir namespacing
Each strategy must publish to distinct topics so consumers/dashboard can tell them apart:
```
STRATEGY_VOTE_TOPIC=market:strategy:votes:v1:scalper
TRADE_SIGNAL_TOPIC=market:strategy:signals:v1:scalper
STRATEGY_POSITION_TOPIC=market:strategy:positions:v1:scalper
STRATEGY_RUN_DIR=/app/.run/strategy_app_scalper
```
Today these default to shared topics. Needs a clean per-strategy namespace convention
(prefix or suffix), and `run_id` should embed the strategy name.

### 3.2 Per-strategy consumer lock
`strategy_app` uses a single-consumer Redis lock so two instances don't double-process.
Multi-strategy means **one lock per strategy identity**, not one global lock. The lock
key already includes an instance id (`STRATEGY_CONSUMER_LOCK_INSTANCE_ID`) — needs to be
per-strategy and verified that N strategies each hold their own.

### 3.3 Portfolio risk manager (the hard one)
Each strategy has its own `RiskManager` today (per-session loss caps, consecutive losses).
With N strategies there must be a **portfolio-level budget**:
- Capital allocation per strategy (e.g. scalper 60%, lottery 40%).
- A portfolio daily-loss kill switch above the per-strategy ones.
- Prevent two strategies from piling into the same instrument beyond a limit.
- Decide: do strategies share a book or hold independent books? (Independent is simpler
  and matches the "separate funds" model — recommended.)

### 3.4 Execution layer per-strategy
- `execution_app` already subscribes to a signal topic. It must subscribe to **all**
  per-strategy topics (or one wildcard) and tag each broker order with the strategy
  (`tag=<strategy>_<signal_id[:6]>`).
- `fill_tracker` already keys by `position_id`; add `strategy` to the fill record and the
  Mongo position doc.
- Shadow/paper/kite selection could even differ per strategy (lottery in shadow while
  scalper is live).

### 3.5 Dashboard multi-strategy view
- Strategy selector / overlay; per-strategy P&L, positions, win rate; portfolio total.
- The OPS sim becomes "sim strategy X" with its own config.

### 3.6 Signal de-duplication / conflict
If scalper and lottery both fire PE 54000 at the same bar, that's two independent
positions (fine, separate books) — but the portfolio risk manager must see the combined
exposure. Decide policy: allow (independent books) vs net (shared book).

---

## 4. Migration path (incremental, low-risk)

**Phase 0 (done):** single strategy, mode-switchable (scalper|lottery), sim-comparable.

**Phase 1 — namespacing:** make topic names, run dir, and run_id strategy-aware via env.
No new strategies yet — just prove one strategy runs cleanly under a `<strategy>` namespace.

**Phase 2 — second container, paper:** run scalper (live/paper) + lottery (paper) side by
side, each own namespace + book. Dashboard shows both. No portfolio risk yet — each keeps
its own caps; total exposure is small (paper).

**Phase 3 — portfolio risk manager:** introduce capital allocation + portfolio kill switch
+ per-instrument aggregate limit. This gates going multi-strategy *live*.

**Phase 4 — execution tagging + reconciliation:** per-strategy broker tags, per-strategy
fill tracking, daily reconciliation by strategy.

**Phase 5 — N strategies live:** add strategies as config-only container definitions.

---

## 5. Stories (high level — to be expanded when this becomes active)

### Epic MS — Multi-Strategy Platform

- **MS-S1** Strategy-aware namespacing (topics, run dir, run_id) via env. *(3)*
- **MS-S2** Per-strategy consumer lock; verify N instances coexist. *(3)*
- **MS-S3** Second strategy container in compose (lottery), paper, own book. *(3)*
- **MS-S4** Dashboard multi-strategy view (per-strategy + portfolio totals). *(5)*
- **MS-S5** Portfolio risk manager: capital allocation + portfolio kill + per-instrument cap. *(8)*
- **MS-S6** Execution per-strategy tagging + fill attribution + reconciliation. *(5)*
- **MS-S7** Per-strategy adapter selection (lottery shadow while scalper live). *(3)*
- **MS-S8** Config schema for "a strategy" (one file/section defines name, profile, exit
  mode, capital %, adapter). *(3)*

### Definition of Done (epic-level)
1. Two strategies run concurrently in paper for 5 trading days with **zero** cross-talk
   (no shared run_id, no double-processed snapshots, separate position books).
2. Dashboard shows per-strategy and portfolio P&L correctly.
3. Portfolio kill switch halts *all* strategies; per-strategy caps still work.
4. Adding a third strategy requires **only** a compose env block + a profile — no code.
5. Reconciliation matches per-strategy fills to per-strategy Mongo positions.

---

## 6. Design guardrails (so it stays loosely coupled)

- A strategy is **defined by config, not code.** "Add a strategy" = new env block +
  profile + (optionally) a new exit mode. If adding a strategy needs engine edits, the
  abstraction is wrong.
- Strategies **never reference each other.** They only see market data and their own book.
  Coordination happens at the portfolio risk manager and execution layer.
- The bus stays the contract. New strategy = new subscriber + new namespaced publisher.
- Keep books independent unless there's a proven reason to net — independent books are
  simpler to reason about, attribute, and kill.

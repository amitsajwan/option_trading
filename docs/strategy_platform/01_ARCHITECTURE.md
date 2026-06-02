# 01 — Architecture

*How the system is wired today, the two governing principles (loose coupling +
config-driven), and the rules that keep simulation faithful to live.*

---

## 1. Component topology (today)

```
                          Redis pub/sub (the bus)
                                  │
  Kite ─► ingestion_app ─► snapshot_app ─► market:snapshot:v1 ─┬─► strategy_app
                                                               │       │ evaluates each snapshot
                                                               │       │ emits votes/signals/positions/traces
                                                               │       ▼
                                                               │   market:strategy:*:v1 ──► strategy_persistence_app ─► MongoDB
                                                               │                          └─► execution_app ─► broker (paper|kite|shadow)
                                                               │                                         └─► execution:fills:v1 ─► fill_tracker ─► MongoDB
                                                               └─► persistence_app ─► MongoDB (snapshots)

  dashboard (FastAPI + JSX) reads JSONL (.run) + MongoDB + Redis; hosts the OPS sim.
```

Every box is a separate container (`docker-compose.yml`). They share:
- the **Redis bus** (pub/sub topics + streams),
- the **`.run/` volume** (append-only JSONL — the system-of-record per ARCHITECTURE.md §9),
- **MongoDB** (queryable history).

### Key files
| Concern | Path |
|---|---|
| Engine entry/direction/consensus | `strategy_app/engines/deterministic_rule_engine.py` |
| Direction consensus + regime veto | `strategy_app/engines/direction_consensus.py` |
| Strike selection (smart_strike) | `strategy_app/signals/option_selector.py` |
| Position lifecycle + exit chain | `strategy_app/position/tracker.py` |
| Exit policies (scalper + lottery) | `strategy_app/position/exit_policy.py` |
| Risk manager | `strategy_app/risk/manager.py` + `risk/risk_calculator.py` |
| Profiles (strategy + risk config) | `strategy_app/engines/profiles.py` |
| Startup, run_id, ops_env.json | `strategy_app/main.py` |
| Broker adapters | `execution_app/adapter/{base,paper,kite,shadow}.py` |
| Order mgmt + fills | `execution_app/{consumer,order_manager,fill_tracker}.py` |
| Alerts | `execution_app/alerts.py` |
| OPS sim backend | `market_data_dashboard/routes/ops_routes.py` |
| OPS sim UI | `market_data_dashboard/static/webapp/ops.jsx` |

---

## 2. Principle 1 — Loose coupling

**Rule: components communicate over the bus and storage contracts, never direct imports
across service boundaries.**

- `strategy_app` publishes `TradeSignal` events to `market:strategy:signals:v1`. It does
  not know who consumes them.
- `execution_app` subscribes to that topic. Swapping paper↔kite↔shadow is an env var
  (`EXECUTION_ADAPTER`), not a code change.
- The snapshot topic is fan-out: **any number of consumers can subscribe to the same
  market data.** This is the hook for [multi-strategy](03_MULTI_STRATEGY_PLATFORM.md) —
  N strategy containers, each its own consumer.

**Contract events** (defined in `contracts_app/events.py`): snapshot, strategy_vote,
trade_signal, strategy_position, decision_trace, fill. Each is an envelope with
`event_type`, `event_version`, `metadata` (carries `run_id`). Additive, versioned.

---

## 3. Principle 2 — Config-driven

**Rule: behaviour changes via env vars. No code edit to change a threshold.**

Every tunable is an env var read at startup (see [Config Reference](05_CONFIG_REFERENCE.md)).
The exit policies, entry gates, strike tiers, risk limits, and the strategy *mode*
itself (`EXIT_STRATEGY_MODE`) are all env-selected.

The payoff: the OPS tool can run a *different* configuration as a sim by passing env
overrides, without rebuilding anything. The same mechanism is how multiple strategies
will each get their own config in [project #03](03_MULTI_STRATEGY_PLATFORM.md).

**Anti-pattern to watch for:** a hardcoded constant that should be config. Example fixed
this cycle — exit policy thresholds were hardcoded, now `EXIT_*` / `LOTTERY_*` env vars.

---

## 4. The exit-policy architecture (the part most likely to be extended)

```
ExitPolicy (ABC)                       strategy_app/position/exit_policy.py
  .check(position, snap) -> Optional[ExitReason]
  .name -> str

CompositeExitPolicy([...])             first policy to return non-None wins

Scalper stack  (build_scalper_exit_stack):
  ThesisFailPolicy → TrailingStopPolicy → PremiumTargetPolicy

Lottery stack  (build_lottery_exit_stack):
  HardStopPolicy → ThesisFailPolicy → MomentumReversalPolicy
                 → BigTargetPolicy → RunnerTrailPolicy → TimestopPolicy

build_default_exit_stack()  reads EXIT_STRATEGY_MODE, returns the right stack.
```

### Critical interaction with the tracker
`tracker.py` has **legacy inline exits** (stagnant, thesis_fail, premium_stop, max_hold,
underlying_stop…). The exit stack and these inline exits coexist by a rule:

- **Scalper mode:** stack runs first; inline exits remain as complementary stop/timestop
  backstops (the scalper stack has no hard stop of its own).
- **Lottery mode:** the stack is the **sole discretionary authority** — inline exits are
  suppressed (`_stack_active` gate in `tracker.update()`), because the lottery stack
  carries its own `HardStop` + `Timestop`. Only hard safety floors remain
  (forced / risk_breach / hard_close / soft_close).

This is *the* subtle invariant. If a future exit mode is added, decide explicitly
whether it is authoritative (suppress inline) or additive.

### To add a new exit policy
1. Subclass `ExitPolicy`, implement `check` + `name`.
2. Add a unit test in `strategy_app/tests/test_exit_policy.py` (math + boundaries).
3. Add it to a stack builder, gated by env vars with safe defaults.
4. If a new *mode*, extend `build_default_exit_stack()` and decide the inline-exit rule.

---

## 5. Sim fidelity — the rules that make simulation trustworthy

A sim is only useful if it reproduces live. These are hard requirements, learned the
hard way (see [Findings §4](00_CONTEXT_AND_FINDINGS.md)):

1. **Identical code.** The sim imports the same `strategy_app` modules as live.
2. **Identical ML libraries.** `market_data_dashboard/requirements.txt` pins the ML stack
   (numpy/pandas/scikit-learn/joblib/lightgbm/xgboost) to *exactly* match
   `strategy_app/requirements.txt`. Drift changes model predictions silently.
3. **Identical config baseline.** `strategy_app/main.py` writes `ops_env.json` to
   `.run/strategy_app/` at startup with its real env. The sim reads that as the baseline;
   operator overrides layer on top. The sim must **not** read config from its own process env.
4. **Merge, don't overwrite, profile config.** When building run metadata, merge run
   overrides onto the profile's `risk_config` (preserves `allow_non_atm_for_ml_entry`,
   `atm_strike_only`, etc.). `main.py` does this; the sim must mirror it.
5. **Sim never writes to live state.** Sims run with `STRATEGY_RUN_DIR=/tmp/sim_<id>`
   (forced, not `setdefault`) and `STRATEGY_REDIS_PUBLISH_ENABLED=0`. Any reader of live
   data must exclude `run_id` starting with `sim`.

**Validation gate:** before trusting any new sim path, run it on a known day with no
overrides and confirm it reproduces the live result for that day to the decimal.

---

## 6. Where the data lives (so a new sim can find it)

| Data | Location | Notes |
|---|---|---|
| Today's live snapshots | `.run/snapshot_app/events.jsonl` | one JSON snapshot per line, intraday |
| Historical snapshots | `.data/ml_pipeline/parquet_data/snapshots/year=YYYY/...` | built overnight; the multi-day sim source |
| Live positions/trades | `.run/strategy_app/positions.jsonl` | POSITION_OPEN/MANAGE/CLOSE |
| Decision traces | `.run/strategy_app/decision_traces.jsonl` | one per evaluate() |
| Mongo collections | `strategy_positions`, `trade_signals`, `strategy_votes`, `phase1_market_snapshots` | queryable history |
| ML models | `ml_pipeline_2/artifacts/{entry_only,direction_only,option_pnl_bundles}/...` | mounted read-only |

Today's parquet is **not** available until the overnight build — that's why the OPS
"sim today" reads `events.jsonl` directly. The multi-day sim reads parquet (see #02).

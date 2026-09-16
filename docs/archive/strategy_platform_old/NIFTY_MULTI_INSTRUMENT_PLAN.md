# NIFTY / Multi-Instrument Plan

**Date:** 2026-06-26 · **Status:** plan (no code changes yet) · companion to DHAN_MIGRATION_EXECUTION_PLAN.md

## 0. Why NIFTY (the opportunity)

NIFTY **weekly** options are still listed (BankNifty weeklies were discontinued ~Nov 2024). So NIFTY
gives **5 full years of the gamma-rich weekly instrument** — the exact instrument the intraday
buying edge depended on (positive-skew lottery on outlier trend days lives on weekly gamma). NIFTY
is likely a **better** home for this strategy than monthly BankNifty. Prioritize it.

---

## 1. How NIFTY flows end-to-end — what's already seamless

| Stage | NIFTY path | Already works? |
|---|---|---|
| **Historical data** | `dhan_data_pipeline fetch --instrument NIFTY` (INSTRUMENTS has NIFTY: secId 13, strike 50, lot 75; option fetch uses `expiryFlag=WEEK` → 5yr weekly) | ✅ turnkey |
| **Features** | `feature_engine.build_features` — instrument-agnostic (any OHLC/OI/IV) | ✅ |
| **Training** | same pipeline → `snapshots_nifty_v1` → a `nifty` recipe | ✅ (needs recipe) |
| **Live feed** | `BROKER=dhan` + `INSTRUMENT_SYMBOL=NIFTY…`; Dhan WS/REST per instrument | ✅ |
| **Redis tick/chain/ohlc keys** | `websocket:tick:{instrument}:latest`, `options:{instrument}:chain` — instrument is **in the key** | ✅ no collision |
| **Snapshot/strategy topics** | `LIVE_TOPIC`, `STRATEGY_VOTE/SIGNAL/POSITION_TOPIC` — env-configurable, **global defaults** | ⚠️ must set distinct per instrument |
| **Dashboard** | reads `INSTRUMENT_SYMBOL` (`app.py:187`) | ⚠️ partial — has BANKNIFTY defaults/labels |
| **SIM/Replay** | `resolve_namespace("sim", run_id)` isolates by run_id; instrument comes from the snapshot data | ✅ |
| **Models** | per-instrument model path (`ENTRY_ML_MODEL_PATH`) | ✅ separate models |

**"Same code, different containers" — yes.** Same `snapshot_app`/`strategy_app`/`ingestion_app`/
`execution_app` images; a second container per instrument, configured by env. No code fork for the
happy path. The gaps below are the exceptions.

---

## 2. Hardcoding audit — the real answer to "are there hardcodings?"

| Hardcoding | Where | Impact on NIFTY | Fix |
|---|---|---|---|
| **Lot size** `BANKNIFTY_LOT_SIZE` (env-configurable but misnamed + single global) | `strategy_app/constants.py:12`, `risk_calculator.py:67`, `risk/manager.py`, `market_snapshot.py:899` | NIFTY lot=75 vs BankNifty=30 → **wrong position sizing / risk** unless env overridden | rename → `INSTRUMENT_LOT_SIZE` (or per-instrument from INSTRUMENTS); set per container |
| **Expiry calendar** monthly-only | `dhan_data_pipeline._build_monthly_expiry_calendar` | NIFTY is **weekly** (Thursday) not monthly | instrument-aware expiry (weekly/monthly cadence) |
| **`--start-date 2024-11-01`** monthly scoping default | `dhan_data_pipeline build` | NIFTY uses **full 5yr** (no discontinuation) | per-instrument default (NIFTY = no cutoff) |
| **Cross-asset basis** `NSE:NIFTY BANK` pair | `market_snapshot.py:833/881/1895…` | BankNifty-specific; for NIFTY computes irrelevant basis | gated by `CROSS_ASSET_ENABLED` (off by default) — leave off for NIFTY, or generalize the pair |
| **Topics global defaults** | `contracts_app/topics.py` | two instruments on one Redis → snapshot/vote/signal **collision** | §3 |
| **Dashboard defaults/labels** `BANKNIFTY-I`, `"live · BANKNIFTY"` | `market_data_dashboard/real_source.py:1003`, `routes/monitor_ws.py:125`, `ops_routes.py:573` | cosmetic — shows "BANKNIFTY" for a NIFTY session | drive label/defaults from `INSTRUMENT_SYMBOL` |
| **Default instrument** `BANKNIFTY-I` | `snapshot_batch_runner.py:48`, batch defaults | overridable; just a default | pass `--instrument` explicitly |

**Verdict:** no *blocking* hardcoding — everything is overridable by env today — but **lot-size** and
**expiry** are correctness-critical (get them wrong and NIFTY sizing/DTE features are wrong), and
**topics** are the seamless-coexistence crux.

---

## 3. Topic / namespace strategy (running NIFTY + BankNifty together)

The snapshot + strategy topics are env-configurable but share global defaults, so two instrument
containers on the same Redis would cross-contaminate. Two ways:

- **A — per-container env discipline (works today):** each instrument container sets
  `LIVE_TOPIC`, `STRATEGY_VOTE_TOPIC`, `STRATEGY_SIGNAL_TOPIC`, `STRATEGY_POSITION_TOPIC`,
  `STRATEGY_DECISION_TRACE_TOPIC` and the sim consumer namespace to instrument-suffixed values
  (e.g. `…:nifty:v1`). Zero code; pure config; error-prone if forgotten.
- **B — auto-namespace by instrument (recommended):** derive the topic suffix from
  `INSTRUMENT_SYMBOL` automatically in `contracts_app/topics.py` (one small change). Then a new
  instrument is genuinely just `INSTRUMENT_SYMBOL=…` with no topic bookkeeping. This is the
  "instrument-pluggable" analogue of the broker registry.

Recommend **B** for true seamlessness; **A** is the stopgap for a quick first NIFTY run.

---

## 4. Instrument-pluggability principle (mirror the broker work)

Goal: **adding an instrument = config + model, no app-code change** (same rule we just applied to
brokers). Single source of truth = the `INSTRUMENTS` config (security IDs, strike step, lot size,
**expiry cadence weekly|monthly**). Everything instrument-specific (lot, expiry, topics, dashboard
label) reads from it / from `INSTRUMENT_SYMBOL`. Guard with a test like the broker registry tests.

---

## 5. NIFTY data fetch (point #2 — trigger on the ML VM)

Same `fetch`, different instrument — no code change needed for the fetch itself:
```bash
# on ML VM (tmux; ~hours for 5yr; needs a valid Dhan token)
python -m ml_pipeline_2.scripts.dhan_data_pipeline fetch \
    --instrument NIFTY --start 2021-06-01 --end 2026-06-26 \
    --token "$DHAN_TOKEN" --client-id 1111957145 \
    --out-dir ~/dhan_pipeline_nifty
```
Produces `~/dhan_pipeline_nifty/raw/` (index 13, VIX, NIFTY **weekly** options ATM±5 CE/PE).
Futures: same scrip-master caveat as BankNifty (expired contracts absent) → index volume / VWAP
limitation applies; the `fetch-futures` adapter covers current-only. (NIFTY futures volume for
history has the same wall — VWAP excluded for v1, same as BankNifty.)

---

## 6. Phased plan

| Phase | Work | Code change? |
|---|---|---|
| **P1. Fetch NIFTY data** | `fetch --instrument NIFTY` 5yr weekly on ML VM (tmux) | none |
| **P2. Instrument-aware expiry + start-date** | weekly/monthly cadence from INSTRUMENTS; NIFTY full-range | small |
| **P3. Build + train NIFTY** | build → `snapshots_nifty_v1` → `nifty` recipe → HPO | recipe only |
| **P4. Instrument-pluggability cleanup** | lot-size rename, topics auto-namespace (§3B), dashboard label, INSTRUMENTS as single source | medium |
| **P5. Live NIFTY container** | compose service: same images + `INSTRUMENT_SYMBOL=NIFTY`, `BROKER=dhan`, NIFTY model, distinct topics | config |

**P1 can start now** (no code change, just the fetch). P2–P4 are the instrument-pluggability work
(analogous to the broker registry). P5 is deployment.

---

## 6b. Multi-instrument integration — audit + architecture (one app?)

**Question:** one application / one dashboard / one ingestion WS for NIFTY + BankNifty?

### Audit (verified in code)

| Aspect | Today | Multi-instrument implication |
|---|---|---|
| Ingestion keys | `websocket:tick:{instrument}:latest`, `options:{instrument}:chain` — **namespaced** | ✅ one WS, many instruments (Dhan ≤5000/conn) |
| Topics | `LIVE_TOPIC`/`STRATEGY_*_TOPIC` — global default, env-settable | ⚠️ must namespace by instrument |
| **Strategy state** | `PositionTracker._position` = **one** open position; engines per-instance — **single-instrument** | ❌ one process = must key ALL state by instrument |
| **Decision traces** | `BaseDecisionEvent` 8 fields — **no `instrument`**; `signal_logger` → one `STRATEGY_RUN_DIR` (votes/signals/positions/decision_traces.jsonl) | ❌ one process = traces **mix instruments → confusion** |
| Mongo persistence | `mongo_sink` writes `instrument` + `underlying_symbol`, indexed | ✅ already instrument-tagged |

**Conclusion:** the **data plane is integration-ready** (namespaced keys, instrument-agnostic
feature_engine, instrument-tagged Mongo). The **trading/trace plane is single-instrument** — state
isn't keyed and **traces have no instrument tag and share one run-dir**. Forcing strategy into one
process would *create* the trace confusion you want to avoid.

### Recommended architecture — shared data plane, isolated trading plane

```
                ┌──────────────────────────────────────────────────────────┐
  Dhan WS ─────▶│  ONE ingestion_app — multi-instrument WS (NIFTY+BANKNIFTY+ │
  (1 conn,      │  VIX) → Redis keys websocket:tick:{instr}, options:{instr} │
   many instr)  └───────────────────────────┬──────────────────────────────┘
                                            │
                ┌───────────────────────────▼──────────────────────────────┐
                │  ONE snapshot_app — loops the INSTRUMENTS list, builds a   │
                │  snapshot per instrument → topic market:snapshot:{instr}:v1│
                └─────────┬───────────────────────────────┬────────────────┘
                          │ (per-instrument topic)         │
            ┌─────────────▼──────────┐        ┌────────────▼─────────────┐
            │ strategy (NIFTY)        │        │ strategy (BANKNIFTY)      │   ◀── ISOLATED
            │ run_dir=.run/nifty      │        │ run_dir=.run/banknifty    │      (own book,
            │ topics …:nifty:v1       │        │ topics …:banknifty:v1     │       run-dir,
            └─────────────┬──────────┘        └────────────┬─────────────┘       namespace)
                          └───────────────┬────────────────┘
                ┌─────────────────────────▼──────────────────────────────┐
                │  ONE dashboard — per-instrument panels; reads each       │
                │  instrument's namespaced topics/run-dir/Mongo (instr key)│
                └──────────────────────────────────────────────────────────┘
```

**Why isolate the trading plane:** real-money blast-radius (a NIFTY bug must not touch BankNifty's
book) **and** clean traces by construction — each instrument's votes/signals/positions/traces live
in its **own run-dir + namespace**, so there is *zero* mixing. You still get one app to *operate* and
one dashboard to *watch*; only the risk-bearing consumer is per-instrument.

### "Clean traces / no confusion" — the guarantees (do these regardless)

1. **`instrument` becomes a first-class field on `BaseDecisionEvent`** (and every signal/position/
   trace record) — so every trace is self-identifying. Cheap, high-value, needed even for the shared
   dashboard to label/filter. *(This is the one change that most directly kills confusion.)*
2. **Per-instrument run-dir** (`STRATEGY_RUN_DIR=.run/strategy/{instrument}`) — JSONL traces never
   share a file across instruments.
3. **Per-instrument topics + sim namespace** (auto-suffix by instrument) — Redis streams don't cross.
4. **Dashboard partitions by instrument** — instrument is the top-level selector/column; ops views,
   KPIs, decision-trace viewer all filtered by instrument. No global "BANKNIFTY" labels (fix the
   hardcoded ones in §2).
5. **Mongo** already tags instrument — add the instrument filter to every ops query/view.

### Phased (folds into §6)
- **MI-1** Add `instrument` to `BaseDecisionEvent` + trace/signal records + per-instrument run-dir. *(no behavior change; pure observability hygiene — do first)*
- **MI-2** Auto-namespace topics + sim namespace by `INSTRUMENT_SYMBOL`.
- **MI-3** snapshot_app loops the instrument list (one app, N instruments) + one WS subscribing to all.
- **MI-4** Dashboard multi-instrument panels (instrument as partition key); fix hardcoded labels.
- **MI-5** Strategy stays **per-instrument** (own run-dir/namespace/container) — no state-keying refactor, no shared-process real-money risk.

---

## 7. Risks / watch-items
- **Topic collision** if P4/§3 not done and per-container topic env forgotten → cross-instrument leakage.
- **Lot-size** must be set/derived per instrument or NIFTY risk sizing is wrong (uses 30 not 75).
- **VWAP/volume** same historical-futures wall as BankNifty (index volume only) → v1 excludes VWAP.
- **Migration ≠ edge** still holds — NIFTY weekly is the *better* shot at the buying edge, not a guarantee.

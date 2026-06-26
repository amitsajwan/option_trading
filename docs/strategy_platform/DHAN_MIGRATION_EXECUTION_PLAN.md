# Dhan Migration + Feature-Engine Unification — Execution Plan

**Status doc — single source of truth for "where we are, how it works end-to-end, who does what".**
Companion to [DHAN_API_MIGRATION.md](./DHAN_API_MIGRATION.md) (the API reference). Where the
two disagree, **this doc wins** — the API doc predates the feature-engine work and still says
"snapshot_app unchanged", which is no longer true (see §2).

**Date:** 2026-06-26 · **Branch:** `feat/dhan-feature-engine` · **Owner:** (assign)

---

## 0. TL;DR for the team

We are doing **two things at once**, and they meet in one place:

1. **Source swap** — Kite → Dhan for both live feed and 5-year historical (training) data.
2. **Feature unification** — collapse the *definitions* of every derived feature into **one
   module, `feature_engine.py`**, used identically by training and all live paths. This kills
   "train/serve skew" (the model seeing different numbers live than it trained on) — the single
   biggest recurring bug in this system.

The unifying idea: **core data (OHLC/OI/IV) comes from Dhan; one transformation library turns it
into features; training and live both call that same library.** Same input → same features →
no skew, by construction.

---

## 1. Where we are RIGHT NOW (honest status)

### ✅ Done + tested + committed (branch `feat/dhan-feature-engine`)

| Item | Commit | Proof |
|---|---|---|
| `feature_engine.py` — 7-layer single feature pipeline, full contract coverage (163 cols) | `ba056c1`, `29ac580` | 16 unit tests: causality, alias-equiv, idempotency, coverage |
| Training pipeline (`dhan_data_pipeline.py`) wired to `feature_engine` | `d30d711`, `ba056c1` | builds `snapshots_dhan_v1` |
| Dhan live feed client + service (`dhan_client.py`, `dhan_data_service.py`) | `d30d711` | auto-selected when `DHAN_ACCESS_TOKEN` set |
| Velocity computed **per-bar from 09:15** (11:30 restriction removed) | `d30d711`, `b46f6b1` | velocity-state tests updated |
| Parity harness: `feature_engine` ≡ legacy live path, 31 cols exact | `40dabf0` | `test_feature_engine_parity.py` |
| Convergence: `_add_group_features` + `RollingFeatureState` fixed to match `feature_engine` (5 skews) | *(uncommitted, in progress)* | parity test now 0-divergence on ALL cols |

### 🔄 In progress (this session)

- Convergence of the live paths (the 5 skew-fixes) — code done, **final full-suite run + commit pending**.
- Live A (`market_snapshot.prepare_market_snapshot_window`) `dist_from_day_*` fix — **edited, not yet tested/committed**.

### ⛔ Not started

- Dhan **token automation** (`dhan_totp_auth.py`).
- Dhan **WebSocket live feed** (current `dhan_data_service` is REST/poll).
- **5-year historical backfill** run on the ML VM (fetch → build → assemble).
- **Retrain** models on `snapshots_dhan_v1` + **publish**.
- **Deploy** (rebuild images, SIM-validate, cutover).

### 📍 The branch is NOT merged. Real money is OFF. Nothing is deployed.

---

## 2. The end-to-end architecture (corrected)

```
                         ┌──────────────────────────────────────┐
   Dhan APIs ───────────▶│   CORE DATA  (OHLC, OI, IV, VIX)      │
   (live + historical)   │   per 1-min bar, BankNifty            │
                         └───────────────────┬──────────────────┘
                                             │
                            ┌────────────────▼─────────────────┐
                            │   feature_engine.build_features() │   ◀── ONE definition of every
                            │   L0 normalise → L1 returns →     │       derived feature
                            │   L2 technicals → L2b flow →      │
                            │   L3 session → L4 velocity →      │
                            │   L5 compression → L6 context     │
                            └───────┬───────────────────┬───────┘
                                    │                   │
                   ┌────────────────▼──┐         ┌──────▼─────────────────────────┐
                   │ TRAINING (batch)   │         │ LIVE (3 paths, parity-locked)  │
                   │ dhan_data_pipeline │         │  A market_snapshot (futures_   │
                   │ → snapshots_dhan_v1│         │    derived → strategy heuristics)│
                   │ → model training   │         │  B live_ml_flat (→ ML models)  │
                   └────────────────────┘         │  C RollingFeatureState (stream)│
                                                  └────────────────────────────────┘
```

### The key invariant (this is the whole point)

> **Every derived feature has exactly ONE definition, in `feature_engine.py`. Training and all
> three live paths produce byte-identical values for the same input. CI parity tests enforce it.**

Why three live paths still exist (not collapsed to one): they have different **performance/shape**
needs — batch full-day (B), incremental O(1)-per-tick streaming (C), snapshot assembly (A). They
are kept as separate *implementations* but **proven identical** to `feature_engine` by parity tests.
That's defense-in-depth, not duplication. `feature_engine` is the **definition**; the live paths are
**conformant implementations**.

### What changed vs the old API doc's claim

The old doc said "Only `ingestion_app/` changes; snapshot_app/strategy_app unchanged." **That was the
pre-feature-engine plan.** Reality: we also unified `snapshot_app/core/` (feature_engine,
runtime_features, market_snapshot, velocity) and `strategy_app/ml/rolling_feature_state.py`. This was
necessary — the skew lived in those files. It is a *contained, test-guarded* change.

---

## 3. End-to-end data flow — the two journeys

### Journey 1 — TRAINING (offline, ML VM)
```
Dhan historical APIs                                  ml_pipeline_2/scripts/dhan_data_pipeline.py
  /v2/charts/intraday      (futures, VIX, 90d chunks)   step: fetch  → raw bars
  /v2/optionchain/expiredOptions (ATM±10, 30d chunks)   step: build  → feature_engine.build_features()
                                                        step: assemble → snapshots_dhan_v1 parquet
                                                                          ↓
                                                        model training (HPO) → entry/direction models
```

### Journey 2 — LIVE (online, GCP runtime)
```
Dhan live (token set)        ingestion_app                snapshot_app                strategy_app
  REST/WS feed  ──────────▶  dhan_data_service ──Redis──▶ market_snapshot (Live A)  ─▶ heuristics
                             (auto-selected when           live_ml_flat (Live B) ──────▶ ML models
                              DHAN_ACCESS_TOKEN set)        RollingFeatureState (Live C)─▶ stream ML
                                                            ↑ all call feature_engine defs
```

**The contract that ties them:** `snapshot_ml_flat` (the ML flat-row schema). `feature_engine`
produces every one of its `REQUIRED_COLUMNS_V2` feature columns. Validation fails loudly if a
column is missing.

---

## 4. Work breakdown — assignable to the team

Workstreams are ordered by dependency. **WS-A and WS-D can start in parallel today.** WS-E (retrain)
depends on WS-D (data). WS-F (deploy) depends on everything.

### WS-A · Dhan live feed (WebSocket) — *can start now*
**Owner:** ___ · **Depends on:** nothing · **Files:** `ingestion_app/dhan_client.py`, `dhan_data_service.py`
- Replace REST poll with `dhanhq.MarketFeed` WebSocket (binary parser) for futures + VIX tick.
- Keep REST `optionchain` for the full chain (1 call/min/expiry).
- Security-ID lookup: download instrument CSV each morning, cache in Redis.
- **Acceptance:** live snapshot fields (`fut_*`, `atm_*`, `pcr`, `vix`) populate identically to Kite
  for a parallel-run day; `dhan_data_service` passes the same interface contract as `KiteDataService`.

### WS-B · Token automation — *can start now*
**Owner:** ___ · **Depends on:** Dhan TOTP secret · **Files:** new `ingestion_app/dhan_totp_auth.py`
- `POST /app/generateAccessToken` (TOTP+PIN) at 08:30 IST; `/v2/RenewToken` fallback; 401 auto-retry.
- **Acceptance:** fresh token daily with no manual step; rotates the chat-pasted token out.
- **SECURITY:** never log/print tokens; store secret in `/opt/option_trading/.kite_secrets`.

### WS-C · Finish feature convergence — *in progress, ~done*
**Owner:** (current) · **Depends on:** nothing · **Files:** `feature_engine.py`, `runtime_features.py`, `rolling_feature_state.py`, `market_snapshot.py`
- 5 skew-fixes applied (ema_9_21_spread, rsi via shared primitive, dist_from_day ×2, iv_skew).
- Live A `dist_from_day_*` fix (edited, needs test).
- **Acceptance:** parity tests assert 0-divergence on ALL shared columns; full snapshot_app +
  strategy_app suite green. **← blocking gate before any retrain/deploy.**

### WS-D · 5-year historical backfill — *can start now, ML VM*
**Owner:** ___ · **Depends on:** WS-B (token) · **Files:** `dhan_data_pipeline.py` (fetch/build/assemble)
- Fetch BankNifty fut + VIX intraday (90d chunks) + expired options (30d chunks).
- `build` → `feature_engine` per day; `assemble` → `snapshots_dhan_v1`.
- **Verify each step** (the pipeline has per-step verification — use it; check completeness per expiry).
- **Acceptance:** `snapshots_dhan_v1` covers 2021–2026, passes schema validation, no day gaps;
  spot-check 5 days vs known market moves.

### WS-E · Retrain + publish — *depends on WS-C + WS-D*
**Owner:** ___ · **Files:** ML VM training, `docs/MODELS_INDEX.md`
- Train entry/direction on `snapshots_dhan_v1`. Isotonic calibration + data-driven threshold.
- **Acceptance:** OOS AUC ≥ current; drop-top-N robustness across months; publish bundle to GCS.
- 3 pending models to gate-check first: `entry_early_trend_v1`, `entry_allsession_bmm_v1`, `entry_allsession_full_v1`.

### WS-F · Deploy + cutover — *depends on all*
**Owner:** ___ · **Files:** `.env.compose`, images
- **ATOMIC cutover:** feature_engine-live + dhan-trained model ship together (never new feature code
  with an old model, or vice versa).
- **Acceptance:** SIM run on rebuilt image reproduces expected features; parallel Dhan+Kite 2–3 days;
  real money stays OFF until net-positive gate met.

---

## 5. Non-negotiable rules (learned the hard way — see memory)

1. **ALWAYS rebuild the Docker image to deploy. NEVER `docker cp`.** docker-cp reverts on recreate,
   AND **SIM runs IMAGE code, not source** — cp'd changes are untested in SIM. Rebuild
   `strategy_app` + `strategy_app_sim` + `ingestion_app`, recreate, re-verify from the running container.
2. **Parity tests are CI gates, not optional.** If `test_feature_engine_parity.py` or
   `test_feature_parity_batch_vs_stream.py` fails, a feature definition drifted — **stop and fix**, do
   not override. This is the skew alarm.
3. **Model + feature code cut over atomically.** A model trained on `feature_engine` MUST be served by
   `feature_engine`-conformant live code. Mismatched halves = silent skew = the bug we're killing.
4. **Verify config from the RUNNING container**, not source. There are two `.env.compose` files; the
   wrong one reverts to dangerous defaults.
5. **Real money OFF** until a net-positive, drop-top-N-robust result on live-regime paper. No exceptions.
6. **Rotate any chat-pasted token.** Never commit/log/print `DHAN_ACCESS_TOKEN`.

---

## 6. Glossary (so the team speaks one language)

- **feature_engine** — `snapshot_app/core/feature_engine.py`. The one library of feature definitions.
- **train/serve skew** — model sees different feature values live than in training. The thing we kill.
- **parity-locked** — a live implementation proven byte-identical to feature_engine by a CI test.
- **`snapshots_dhan_v1`** — the new Dhan-sourced training parquet, built by feature_engine.
- **`snapshot_ml_flat`** — the ML flat-row contract; defines the columns models consume.
- **Live A / B / C** — market_snapshot (heuristics) / live_ml_flat (batch ML) / RollingFeatureState (stream ML).
- **Layers L0–L6** — normalise → returns → technicals → flow → session → velocity → compression → context.

---

## 7. Open questions for the team / decisions pending

1. WebSocket: official `dhanhq` SDK vs custom binary parser? (Recommend SDK first — WS-A.)
2. Greeks (delta/theta/vega) — Dhan provides them, Kite didn't. Add as model features in WS-E? (Evaluate, don't block.)
3. Merge strategy for `feat/dhan-feature-engine` — single PR or split (feature-engine vs Dhan-feed)?
4. Who owns the ML VM during backfill+retrain (WS-D, WS-E) — they share the box.

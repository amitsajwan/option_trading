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
| `snapshot_app` suite green — feature_engine, runtime_features, market_snapshot, velocity, contracts | committed | **152 passed** |
| Velocity computed **per-bar from 09:15** (11:30 restriction removed) | `d30d711`, `b46f6b1` | velocity-state tests updated |
| **Dhan historical FETCH complete** (raw, on ML VM `~/dhan_pipeline/raw/`) | — | 343 MB, 2021-08→2026-06, ATM±5 CE+PE + index + VIX |
| **Token automation** `dhan_totp_auth.py` — client-id + PIN + TOTP → access token; `--dry-run`/`--verify`; `.env.compose` auto-update | committed (Wind) | **9 tests passed** |
| **WS live feed** `dhan_ws_feed.py` — `dhanhq.MarketFeed` futures+VIX → Redis I2 keys; heartbeat; REST fallback; wired into `DhanDataService.get_tick()` | committed (Wind) | **15 tests passed** |

### ⚠️ CORRECTION (must read — supersedes earlier claims in this branch's commit messages)

An earlier commit (`98d6344`) claimed it "fixed 5 latent train/serve skews" including a
`dist_from_day` "sign flip the deployed model was fed wrong." **That was based on an unverified
assumption and it is WRONG.** Verified against `snapshot_app/historical/snapshot_batch.py` (the actual
builder of the deployed model's `snapshots_ml_flat_v2` training data):

- **The deployed system was NOT skewed.** v2 training data and all live paths used the *same* old
  feature forms — they matched. There was/is **no live bug today** on these features.
- **`feature_engine` is a NEW feature convention**, not a reproduction of v2. A full audit shows it
  diverges from the deployed-v2 convention on **17 columns** (EMA spread/slopes, RSI, dist_from_day,
  regime definitions, expiry logic, ATR percentile). See §2b.
- Therefore the "convergence" commit did **not** fix a bug — it **introduced the new convention** into
  the live paths. That is acceptable **only** because the goal is **new models** (below), and only via
  **atomic cutover**. It must **never** be merged to `main`/deployed against the *current* model.

### 🎯 The goal (anchors every decision)

**Train NEW models on Dhan data via `feature_engine`, and cut the live paths over to `feature_engine`
atomically with those new models.** We are *retiring* the v2 model, not preserving it — so
feature_engine's divergence from the v2 convention is fine *by design*. What must hold: **training
(dhan_data_pipeline) and live both use feature_engine** → consistent for the new model.

### ✅ Immediate gate — DONE (feature_engine finalized for the monthly regime)

`build` bakes feature_engine's conventions into the training data, so it was finalized first
(committed on the branch; 64 feature/parity/rolling tests green):
- **Monthly scope**: `dhan_data_pipeline build --start-date` (default `2024-11-01`) → monthly regime only (§8a).
- **Monthly expiry**: last-Thursday calendar (holiday-rolled-back via real trading days; raw data has
  **no expiry column** — so calendar, not data) → passed via `build_features(expiry_date=…)`. Verified
  `ctx_dte_days=23` (correct monthly) vs the weekly heuristic's wrong `1`.
- **Normalization consistency**: `ema_*_slope` now `/close` like `ema_9_21_spread`; converged across all
  3 live paths. (Supersedes the earlier "actual expiry from data" note — data has no expiry column.)

### ✅ `snapshots_dhan_v1` built (2026-06-26, ML VM)

407 trading days · 153,032 bars · 2024-11 → 2026-06 · 331 columns · ~199 MB.
Price / returns / EMA / RSI / ATR / OI / IV / velocity / context / DTE / regime — all 0–2% NaN. Clean.

**Compression features:** present under real names (`bb_width_20`, `range_10/30`, `ema_spread_9_21`,
`compression_score`, `adx_14`, `vol_spike_ratio`). Verify script had stale names — cosmetic fix only.

**⚠️ VWAP/volume limitation (known, non-blocking for v1):**
`vwap_fut` is 61% NaN because the initial fetch used BankNifty *index* (`securityId=25`, IDX_I segment).
Indices have no real traded volume (~88% zeros), so all volume-derived features are degraded:
`vwap_fut`, `vwap_distance`, `ctx_above_vwap`, `fut_flow_rel_volume_20`, `vol_spike_ratio` (partial).

**Fix (futures re-fetch, queued for production model):**
Monthly BankNifty futures carry real traded volume but require stitching ~18 monthly contracts
(security IDs rotate on expiry). Code is now ready:
- `dhan_data_pipeline.py fetch-futures` — downloads Dhan scrip master CSV, resolves monthly
  contract IDs automatically, fetches each contract's active window, stitches to `futures.parquet`.
- `dhan_data_pipeline.py fetch` now also auto-fetches futures (inline, same token).
- `_build_day_indicators` uses `futures.parquet` for `px_fut_*` / VWAP when present, falls back
  to index otherwise. `px_spot_*` always uses the index.

**Decision:** proceed with exploratory training on v1 (VWAP features excluded/noted). Queue futures
re-fetch on ML VM in parallel → rebuild `snapshots_dhan_v1` with real volume → production model.

### ⛔ Not started

- **Futures re-fetch + snapshots_dhan_v1 rebuild** (real volume, VWAP clean):
  ```bash
  python ml_pipeline_2/scripts/dhan_data_pipeline.py fetch-futures \
      --start 2024-11-01 --end 2026-06-26 \
      --token $DHAN_TOKEN --client-id 1111957145 \
      --out-dir ~/dhan_pipeline
  # then re-run build + assemble
  ```
- **Train new models** on `snapshots_dhan_v1` + **publish**.
- **Deploy** (rebuild images, SIM-validate, **atomic** cutover).

### 📍 The branch is NOT merged. Real money is OFF. Nothing is deployed.

**Test note:** `snapshot_app` 152/152 green; `strategy_app` green except one **pre-existing, unrelated**
hang (`test_stage_consumers.py::...test_publishes_strike_event` — an event-bus/Redis consumer test that
blocks on a message; imports no feature modules; hangs in isolation). Not from this work.

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
runtime_features, market_snapshot, velocity) and `strategy_app/ml/rolling_feature_state.py`. This is a
*contained, test-guarded* change — but it ships **only** with the new models (§2b).

---

## 2b. feature_engine is a NEW convention — read before touching features

A full audit (`feature_engine.build_features` vs `snapshot_batch._project_rows_to_ml_flat`, the v2/
deployed-model builder, same input) found feature_engine **diverges on 17 of ~120 columns**. These are
genuine *definitional* choices, not bugs — and **neither convention is internally consistent** (they
made opposite calls):

| Feature group | feature_engine | deployed v2 | Note |
|---|---|---|---|
| `ema_9_21_spread` | /close (normalized) | raw | fe normalizes |
| `ema_9/21/50_slope` | raw | /close (normalized) | **fe inconsistent w/ its own spread** |
| `osc_rsi_14` | clip / NaN-seed | where / 0-seed | fe cleaner (marginal) |
| `dist_from_day_high/low` | (H−c)/c | (c−H)/H | opposite sign |
| `ctx_regime_trend_up/down` | stacked EMA (9>21>50) | spread-sign (≥0) | different signal |
| `ctx_is_expiry / near / regime_expiry` | heuristic Wed/Thu | actual `expiry_code` | **fe wrong on holidays** |
| `ctx_regime_atr_high/low`, `osc_atr_percentile` | expanding median/rank | percentile threshold | method differs |
| `ctx_opening_range_breakout_up/down` | computed from price | upstream/stored flag | path differs |

**Why this is OK:** the goal is **new models**. They train on feature_engine output, so they bind to
feature_engine's convention. v2's convention retires with the v2 model. The 17-column divergence only
matters at the **cutover boundary** — which is why cutover is atomic.

**What we still fix (correctness, not v2-matching):** the holiday-breaking expiry heuristic and the
spread-vs-slope normalization inconsistency. Do this **before** `build`.

**Hard rule:** never run a live path on feature_engine against a model trained on the v2 convention
(or vice versa). That is the skew. Cutover ships feature_engine-live + a feature_engine-trained model
together, or not at all.

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

## 4. Work breakdown — 3 tracks for a 1–2 person team

Sized for 1–2 people: 6 fine-grained workstreams collapsed into **3 sequential-ish tracks**, each
ownable end-to-end by one person. Track 1 and the convergence half of Track 2 can run in parallel;
the rest is naturally sequenced (data → models → deploy).

The **interface contracts** (§4.4) are the seams between tracks — honour those and the tracks
integrate without reading each other's code.

### Track 1 · Dhan Connectivity (live feed + auth)  — *✅ DONE (Wind, 2026-06-26)*
**Owner:** Wind · **Files:** `ingestion_app/dhan_client.py`, `dhan_data_service.py`, `dhan_totp_auth.py` *(new)*, `dhan_ws_feed.py` *(new)*
- ✅ Token automation: `dhan_totp_auth.py` — `POST https://auth.dhan.co/app/generateAccessToken` (client-id + PIN + TOTP); `/v2/RenewToken` thread every 20h; writes `dhan_credentials.json`; updates `DHAN_ACCESS_TOKEN` in `.env.compose`; `--dry-run` / `--verify` CLI. **9 tests**.
- ✅ WebSocket live feed: `dhan_ws_feed.py` — `dhanhq.MarketFeed` (futures Quote + VIX Ticker); reconnect loop with back-off; publishes to I2 Redis keys; heartbeat TTL health check; `DhanDataService.get_tick()` reads WS cache first, falls back to REST if feed stale. **15 tests**. `DHAN_WS_ENABLED=0` disables.
- ✅ REST option-chain poll unchanged (Dhan WS does not stream option chains).
- ✅ `dhanhq>=2.0,<3` added to `ingestion_app/requirements.txt`.
- **Acceptance remaining (needs live VM):** `--dry-run` TOTP code verified; `generateAccessToken` endpoint hit once; parallel-run day Dhan vs Kite tick values match.
- **SECURITY:** `DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET` in `/opt/option_trading/.kite_secrets` — never in repo/chat. `dhan_credentials.json` gitignored.

### Track 2 · Data & Features (finalize feature_engine + build/assemble)  — *fetch DONE; build gated*
**Owner:** ___ · **Files:** `feature_engine.py`, `runtime_features.py`, `rolling_feature_state.py`, `market_snapshot.py`, `ml_pipeline_2/scripts/dhan_data_pipeline.py`
- *(done)* feature_engine built; live paths set to its convention; parity tests 0-divergence.
- *(done)* **Dhan historical fetch** — `~/dhan_pipeline/raw/` on ML VM: 343 MB, 2021-08→2026-06,
  ATM±5 CE+PE + index + VIX. The slow part is banked.
- **GATE — finalize feature_engine BEFORE build** (§2b): fix the heuristic expiry → real expiry from
  data; make spread/slope normalization consistent. `build` bakes these into the training data.
- Then on ML VM: `build` (calls **I3** `feature_engine.build_features` per day → `~/dhan_pipeline/
  indicators/`) → `assemble` → `snapshots_dhan_v1`.
- **Verify each step** — pipeline has per-step verification; check completeness per expiry, no day gaps.
- **Produces (interface I4):** `snapshots_dhan_v1` conforming to `snapshot_ml_flat` schema.
- **Acceptance:** covers 2021-08→2026-06, schema-valid, no gaps; spot-check 5 days vs known moves;
  parity tests stay green (the skew alarm).

### Track 3 · Models & Deploy (retrain + atomic cutover)  — *after Track 2 data + Track 1 feed*
**Owner:** ___ · **Files:** ML VM training, `docs/MODELS_INDEX.md`, `.env.compose`, images
- Gate-check the 3 pending models first (`entry_early_trend_v1`, `entry_allsession_bmm_v1`, `entry_allsession_full_v1`).
- Train entry/direction on `snapshots_dhan_v1` (**I4**); isotonic calibration + data-driven threshold;
  publish bundle (**I5**) to GCS.
- Deploy: rebuild images, SIM-validate, **atomic cutover** (feature_engine-live + dhan-trained model together).
- **Acceptance:** OOS AUC ≥ current + drop-top-N robust across months; SIM on rebuilt image reproduces
  expected features; parallel Dhan+Kite 2–3 days; **real money OFF until net-positive gate met.**

### 4.4 Interface contracts (the seams — honour these to work independently)

**I1 · `DhanDataService` ≡ `KiteDataService`** *(Track 1 → snapshot_app)*
The single seam that lets the feed swap without touching snapshot_app. `DhanDataService` MUST implement,
with identical return shapes to `KiteDataService`:
```
get_tick(instrument)        -> {instrument, timestamp, last_price, best_bid, best_ask, mid, volume, oi}
get_ohlc(instrument, tf)    -> {instrument, timeframe, open, high, low, close, volume, oi, start_at(ISO IST)}
get_options_chain(...)      -> {instrument, expiry, strikes[{strike, ce{ltp,oi,iv,volume,bid,ask}, pe{...}}],
                                 futures_price, pcr, max_pain, atm_strike}
get_depth(instrument)       -> {buy[5], sell[5]}   (stub allowed; Dhan REST has no depth)
health_payload()/system_mode_payload()/list_instruments()  -> same shapes as Kite
```
Auto-selected when `DHAN_ACCESS_TOKEN` is set (see `api_service._build_svc`).

**I2 · Redis keys** *(Track 1 → snapshot_app)* — publish to the SAME keys Kite used:
`websocket:tick:{INSTR}:latest`, `options:{INSTR}:{EXPIRY}:chain`, `ohlc_sorted:{INSTR}:1m`. snapshot_app
reads these unchanged.

**I3 · `feature_engine.build_features(df, *, trade_date, prev_day_close, vix_open, ...)`** *(shared truth)*
The one function training AND live call. Input: 1-min bars with core columns (alias-resolved — training
names `atm_ce_oi` or live-panel `opt_0_ce_oi` both work). Output: df with all derived feature columns.
**Do not compute features anywhere else.** Adding a feature = adding a layer here, once.

**I4 · `snapshots_dhan_v1` schema** *(Track 2 → Track 3)* — every column in
`snapshot_ml_flat_contract.REQUIRED_COLUMNS_V2` present and typed; validated by
`validate_snapshot_ml_flat_rows`. This is the train/serve contract the model binds to.

**I5 · Model bundle** *(Track 3 → live)* — published to GCS in the existing bundle format
(model + calibration + threshold report + feature list). Live loads via `ENTRY_ML_MODEL_PATH`.
The bundle's feature list MUST be a subset of I4's columns (else live can't produce them → skew).

---

## 4c. Broker abstraction — pluggable across all surfaces (2026-06-26)

**Directive:** swapping brokers (kite/zerodha/dhan/future) must NOT change app code or
strategy config — only the broker selection + that broker's credentials.

| Surface | Mechanism | Pluggable |
|---|---|---|
| Live data (ingestion_app) | `BROKER` env → `_MARKET_DATA_SERVICES` registry (`api_service`) | ✅ |
| Execution (execution_app) | `EXECUTION_ADAPTER` → `BrokerAdapter` | ✅ |
| Historical/training fetch (dhan_data_pipeline) | `BROKER` → `_HISTORICAL_ADAPTERS` / `HistoricalDataAdapter` | ✅ |
| Feature computation (feature_engine) | core data → features | ✅ broker-agnostic by design |

**Add a broker** = implement the interface + **one registry line** + set `BROKER=<name>`. No
changes to snapshot_app/strategy_app/feature_engine/models/recipes. `kite`/`zerodha` aliased
(Kite Connect is Zerodha's API). Guard tests: `test_broker_registry.py`, `test_historical_adapter.py`.
**Runtime:** set `BROKER=dhan` in the live `.env.compose` (gitignored).

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
   Corollary: **`feature_engine`-live must NEVER be merged to `main`/deployed against the current v2
   model** — feature_engine uses a different convention (§2b); that pairing is itself the skew.
4. **`build` freezes the convention.** Whatever `feature_engine` computes when `build` runs is baked
   into `snapshots_dhan_v1` and the models trained on it. Finalize feature_engine BEFORE `build`;
   changing a feature definition after `build` means re-running build + retraining.
5. **Verify config from the RUNNING container**, not source. There are two `.env.compose` files; the
   wrong one reverts to dangerous defaults.
6. **Real money OFF** until a net-positive, drop-top-N-robust result on live-regime paper. No exceptions.
7. **Rotate any chat-pasted token.** Never commit/log/print `DHAN_ACCESS_TOKEN`.

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

*Updated 2026-06-26 after convergence landed. Tagged so we can pick work off this list.*

### Resolved this session
- ~~Will the 3 live paths converge to feature_engine with 0 divergence?~~ **Yes** — parity tests 0-divergence on all cols; 5 skews fixed.
- ~~**Q1 — hotfix `dist_from_day` straight onto `main`?**~~ → **No (infeasible as an isolated cherry-pick); it ships via the merge train instead.** See §7.1.
- ~~**Q2 — single PR vs split (feature-engine vs Dhan-feed)?**~~ → **Single PR, stacked on `compression-state-engine`. A clean split is not worth the rebase surgery.** See §7.1.

### 7.1 · Merge / hotfix decision (evidence-based, 2026-06-26)

**Branch topology (verified via `git`):** linear stack —
`main (95c0fc9)` → `feat/compression-state-engine` (regime/entry/exit/tiering, **unmerged**) → `feat/dhan-feature-engine`.
`main` is the merge-base, so it lacks the **entire** compression line.

**Why no isolated `main` hotfix (Q1):**
- The skew fix is commit `98d6344`; it edits `feature_engine.py`, `market_snapshot.py`, `runtime_features.py`, `rolling_feature_state.py` only — **no Dhan-feed files**.
- But `feature_engine.py` **does not exist on `main`**, and the fix binds to it (shared `_rsi`, etc.). `market_snapshot.py` on the branch also carries the compression rewrite (`add_compression_features`) absent from `main`.
- ∴ cherry-picking "just `dist_from_day`" onto `main` would conflict/dangle. **Don't.**

**Why single PR, not a feature-vs-Dhan split (Q2):**
- The first commit `d30d711` already **interleaves** Dhan-feed files (`dhan_client.py`, `dhan_data_service.py`, `dhan_data_pipeline.py`) with shared feature code (`velocity_features.py`, `live_velocity_state.py` — the per-bar/no-11:30 change). Splitting needs interactive-rebase surgery for marginal benefit.
- **Dhan code is inert until `DHAN_ACCESS_TOKEN` is set** (auto-select in `api_service._build_svc`). So shipping it dormant is safe — the convergence/skew fixes deploy, Dhan stays off.

> ⚠️ **CORRECTION (2026-06-26, evidence-based — overrides the merge-train deploy framing below).**
> The audit in §2b proved the convergence is **not a skew-fix** — it's a **convention change**. The
> deployed v2 model and the live paths currently *match* (old forms); there is **no live bug to
> fast-track.** Deploying the feature_engine convention to live **against the still-deployed v2 model
> would CREATE skew** on `dist_from_day` (sign), `ema_spread` (scale), regime/expiry definitions, etc.
> "Parity-locked across the 3 live paths" ≠ "safe against the v2 model." So:
> - **Merging to `main` is fine** — the code can land (Dhan dormant; feature changes present in source).
> - **Deploying the feature changes is NOT** — it must wait for the feature_engine-trained model and
>   ship **atomically** with it (rule #3). There is **no standalone fast-track.**

**Decision — merge train (deploy gated on the new model):**
1. Validate + merge `feat/compression-state-engine` → `main` (its own gate; it's the unmerged base).
2. Merge `feat/dhan-feature-engine` → `main` as a single PR. Dhan feed lands **dormant** (no token).
   The feature_engine convention is now in `main` but **not yet deployed**.
3. **Do NOT rebuild/deploy the feature changes against the v2 model.** Deploy only once the
   feature_engine-trained model is ready, atomically (rule #3). Until then `main` ≠ deployed image.

**Merge dry-run result (Wind, 2026-06-26 — verified, non-destructive):**
- `main` is a **strict ancestor** of `compression`, which is a strict ancestor of `dhan` (0 commits on
  `main` absent from `dhan`). ∴ **both merges fast-forward — zero conflicts to resolve.**
- Sizes: `main..compression` = **152 commits**; `compression..dhan` = **10 commits**.
- **Review note:** keep it a 2-PR train (compression→main, then the 10-commit dhan→main) for digestible
  review, even though git could FF all 162 in one shot.
- **Branch hygiene:** untracked cruft must NOT ride along — `.run_tmp/`, `*.algo-496203.bak` (×2),
  `test_result.txt`, `tmp_chk_bundle.py`. Excluded from any commit.
- **Wind stops at merge** — no rebuild/deploy of the feature changes (per the ⚠️ correction above).

### Decide BEFORE backfill/retrain (Track 2/3)
1. **Backfill token bootstrap** — backfill is blocked on a Dhan token. Use a **manually generated** token to unblock the 5-yr fetch while T1 token-automation lands in parallel, or serialize (T1 fully done → then backfill)?
2. **Dataset identity** — keep new `snapshots_dhan_v1` as a distinct dataset, or land it under the existing `snapshots_ml_flat_v2` name once schema-validated? (Affects every downstream reader / manifest default.)
3. **Retrain scope** — retrain only entry + direction on `snapshots_dhan_v1`, or also the `option_pnl` bundle? (option_pnl is currently set-but-unused in live deterministic path per memory; confirm before spending VM time.)
4. **Velocity-from-09:15 distribution shift** — removing the 11:30 restriction changes velocity feature distributions vs what older models trained on. Confirm retrain consumes the new per-bar velocity (it should, single feature_engine) and that no still-deployed model binds the old 11:30-only definition.

### Evaluate, don't block
5. **WebSocket**: official `dhanhq` SDK vs custom binary parser? (Recommend SDK first — WS-A.)
6. **Greeks** (delta/theta/vega) — Dhan provides them, Kite didn't. Add as model features later? (Evaluate post-cutover; not in the parity contract today.)
7. **ML VM ownership** during backfill+retrain — backfill (WS-D) and retrain (WS-E) share one box; who schedules off-market windows? (oracle labeling needs ≥64 GB RAM per AGENTS.md.)

---

## 8a. DECISION — train on the monthly BankNifty regime only (2026-06-26)

**Finding:** the Dhan options data has a structural break at **~Nov 2024** = the NSE BankNifty
**weekly-options discontinuation**. Before: weekly ATM (DTE 0–7, premium ~250, OI ~3 M). After:
monthly ATM (DTE 0–30, premium ~700–1090, OI ~0.5–1.0 M). Two different instruments under the same
column names. Data itself is clean (0 % NaN / 0 % zero-OI throughout).

**Decision (instrument = BankNifty, which is monthly-only now):** train new models on the **monthly
regime only, 2024-11 → 2026-06 (~1.5 yr)** — *train on what you serve*. The weekly period is a
different instrument; archived, not in the production training set.

**Consequences:**
- `build` scopes to `>= 2024-11-01`. Weekly parquets kept but excluded.
- feature_engine's **weekly Wed/Thu expiry heuristic is wrong here** → compute the **monthly** expiry
  and pass it in (`build_features(expiry_date=…)`; the kwarg already exists). Raw data has no expiry
  column, so derive it from a calendar, not data.
- **Strategy caveat:** monthly ATM = **much lower gamma**. The prior (fragile) weekly buying edge
  (positive-skew lottery on outlier trend days) was gamma-dependent — it will **not** transfer
  unchanged. Treat the retrain as a thesis re-validation, not a model swap. 1.5 yr (~18 expiry cycles,
  ~140 K intraday bars) is thin → watch drop-top-N. Real money stays OFF.
- **Held in reserve:** futures/index/VIX features are the *same* instrument across the full 4.9 yr
  (only options changed tenor). A futures-driven magnitude model *could* use the longer history with
  option-flow features monthly-only — don't complicate the first pass.

---

## 8. Risks, sequencing & data verification (2026-06-26)

### 8.1 Data verification — done, on the ML VM (`~/dhan_pipeline/raw/`)

| Check | Result |
|---|---|
| **Completeness** ✅ | Index 1,223 days / VIX 1,222 / ATM options 1,212 days, 2021-08-04→2026-06-25. Index+VIX have only **2 thin days**; ATM options **0 thin days** (~373 bars/day). ≈4.9 yr as expected. **Good to build.** |
| **Expiry column** ⚠️ | **None.** Raw option parquet cols = `ce_open/high/low/close, ce_iv, ce_oi, ce_volume, spot` — **no expiry date**. So "real expiry from data" is impossible. |
| Earlier "ATM+1 empty" warnings | Evidently transient/recovered — completeness counts are clean. |

**Consequence:** the expiry-correctness fix (§2b) must use the **holiday-aware NSE calendar already in
the repo** (`config/nse_holidays.json`), not the raw data, to replace `_next_weekly_expiry`'s naive
Wed/Thu rule. Probe script: `/tmp/dhan_probe.py` (re-runnable).

### 8.2 Risks the team must hold

1. **Two cutovers, not one — keep them decoupled.** *Feature/model* cutover (feature_engine-live +
   dhan-trained model — atomic, skew-critical) is **independent** of the *feed* cutover (Kite→Dhan).
   feature_engine-live can run on the existing Kite feed. Don't bundle them into one risky deploy.
2. **Migration ≠ edge.** Per our own history (buying has no robust edge; direction is the bottleneck),
   more data + Greeks may *not* yield a profitable model. Dhan migration is **infrastructure**; it does
   not by itself clear the real-money gate. Don't let "retrain → done" creep in.
3. **Validate the data before `build`** ✅ (done, §8.1) — complete, build-ready.
4. **Expiry fix is calendar-based, not data-based** ✅ (resolved, §8.1) — use `nse_holidays.json`.
5. **1–2 people = mostly sequential, multi-week critical path.** finalize fe → build (hrs) → assemble
   → train (days, shared VM) → validate → SIM → cutover. The "3 parallel tracks" framing oversells
   parallelism at this headcount. The ML VM is contended (training sessions already running).
6. **Large unmerged branch = drift risk.** Decide: keep `feat/dhan-feature-engine` branched until the
   new model exists (safer; diverges from main), or merge early **dormant + undeployed** (§7.1). Lean
   keep-branched until the model exists, OR merge but **never deploy the feature changes vs the v2 model**.

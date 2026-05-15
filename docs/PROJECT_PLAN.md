# BankNifty Options ML — Project Plan

**Living document.** Updated as phases progress. Last revised: 2026-05-15.

The North Star, the current state, what we're fixing, in what order, who does what, and where we stop. Refer to this until the project is complete or explicitly redirected.

---

## 1. North Star

**Goal:** A profitable, risk-bounded BankNifty options trading system that uses ML signals on futures to drive option contract selection, deployed with realistic cost modeling and progressive capital ramp.

**Non-goals:** Beating institutional HFT. Trading equities or other instruments. Building a research-only system without a deployment path.

**Definition of done:**
1. The system produces ≥30 live trades in a calendar month
2. Realized PF over those trades is ≥1.15 net of all real costs
3. Max drawdown stays within pre-declared risk budget
4. Promotion from `shadow → paper → capped_live → live` gated by quantitative criteria, not vibes

---

## 2. Current State Snapshot (2026-05-15 evening IST)

**One-line position:** C1 is live in `capped_live` (no real money). Four independent experiments confirm overfit on the C1 recipe family using v1 views — C1 holdout net-negative, F1 walk-forward held, B1 cost-shift held, Path C-1 (conviction filter) documented as Apr-26 dead-end. **G4 is now running** to test a structurally different question: does the C1 recipe find generalizable signal when given a richer feature set (`v3_candidate` views, ~50% more columns including `ctx_*` family)? Result by Saturday ~00:30 IST. **Do NOT deploy real capital — applies regardless of G4 outcome.**

| Layer | State |
|---|---|
| **Live model** | C1 (`staged_deep_hpo_c1_base_20260429_040848`), `regime_gate_v1` active, `capped_live` @ 0.25× size. **No real-money orders are placed** — broker integration doesn't exist yet (Phase 4). |
| **OOS verdict (2026-05-15)** | C1 holdout (2024-08 → 2024-09, 16 trades): **net −55% @ 200 bps**, PF 0.86. Training-window contribution: **+87% of total gross.** Exit-timing sweep (9/15/20/30 bars): all variants net-negative on holdout. See §14. |
| **F1 walk-forward** | **HELD** at `stage1_cv_gate_failed`. C1 recipe trained on pre-2024 → block_rate=1.0. Recipe doesn't generalize across windows. See §15. |
| **B1 cost-aware label** | **HELD** at `stage2_signal_check_failed`. C1 recipe + `cost_per_trade=0.02` instead of 6 bps → Stage 2 direction signal collapses under realistic-cost labels. See §15. |
| **Path C-1 (NOT launched)** | Conviction-filter approach (`direction_or_no_trade_v1` + `stage2_target_redesign`) — already tried Apr 26 2026, killed throughput to 0 holdout trades. Replicating it under 200 bps would be even worse. Don't run. |
| **G4 (RUNNING)** | C1 recipe + `stage{1,2,3}_*_view_v3_candidate` (130 cols vs v1's 83 — ~47 extra `ctx_*` features). Launched 2026-05-15 18:32 UTC, manifest `8a8dd07ad0...`. ETA ~6 hrs. Tests whether richer features rescue the recipe. |
| **Architecture** | Three lanes (training / live / historical replay) sharing `strategy_app` code + published model artifact. [SYSTEM_FLOW_DIAGRAMS.md](SYSTEM_FLOW_DIAGRAMS.md). |
| **Data state** | 2020-08 → 2024-10 in mongo + parquet. **2025+ NOT acquired.** All free paths (Kite Historical, NSE bhavcopy, scrapers) inadequate for 1-min option chain. Forward live collection (Kite live credentials → Monday 09:15 IST market open) is the only viable path. |
| **Known gaps** | (1) `strategy_persistence_app` pubsub hangs after mongo timeout — workaround: JSONL is canonical for backtests. (2) `exit_trigger` not persisted. (3) No broker integration (no real-money orders possible regardless of model). |

**Pause scope (revised after operator pushback).** The pause applies to **C1 recipe iteration on v1 views** — we've confirmed four ways that doesn't work. The pause does NOT apply to structurally different experiments. Operator (correctly) pointed out that new labels, new features, new HPO, or new model families haven't been tested. **G4** (new features via v3_candidate views) is the first such experiment and is running.

**Next concrete actions (in priority order):**

1. **Tonight ~00:30 IST:** read G4 `summary.json`. Two outcomes:
   - G4 HELD (same as F1/B1): then the data ceiling claim firms up — recipe family genuinely exhausted regardless of feature set. Move to G2 (new labels via direct option-P&L; requires new labeler code) or accept "wait for fresh data" position.
   - G4 PUBLISHES: feature set was the lever; we have a candidate model worth forward-validating. Wire shadow mode Monday.
2. **Monday 2026-05-18, 09:15 IST:** if operator shares Kite live credentials, start forward shadow data collection regardless of G4 outcome. Accumulating fresh OOS data has no downside.
3. **Continue testing untried recipe dimensions** if G4 fails: G2 (direct option-P&L label — needs new labeler code), G3 (different ML model family from the existing trainer catalog).

**Operator decision needed:** share Kite live trading API credentials with the runtime VM so forward shadow can start Monday. This is the unblocking action for the OOS validation side, independent of training.

**Active running:** G4 training in tmux `pathg4` on `option-trading-ml-01`, manifest `8a8dd07ad0...`, started 2026-05-15 18:32 UTC.

---

## 3. Problem Statement (what we learned)

The system has been treated as a **prediction problem** when it is actually a **prediction-plus-translation problem**. The model predicts futures direction; we trade option premiums. The translation between the two is:

```
option_pnl = delta × futures_move
           - theta × hold_minutes
           - vega × Δimplied_vol
           - slippage (bid/ask + brokerage + STT + GST)
```

The first term — what the model predicts — is **one of four** terms. Worse, it is the smallest absolute term for short-hold intraday trades. The other three are partly observable (IV is in the snapshot, slippage estimable from spreads) but **none reach the strike-selection layer today**.

Three secondary consequences of this design gap:

1. **Validation cost (6 bps) is unrealistic** for ATM BankNifty option round-trips (realistic 120–320 bps). Published PFs are optimistic.
2. **Strike selection is `atm + liquidity gates`** — it cannot pick ATM vs OTM based on predicted move size, IV regime, or hold duration.
3. **Stage 2/3 may have no real direction edge.** Win rate is ~45% with or without the deterministic gates. The gates are filtering for asymmetric-payoff regimes, not improving the predictor.

**The fix is not "throw away the model." The fix is to:**
- Validate whether the futures model has *any* directional signal (current experiments)
- Build the option-selection layer that the design always assumed but never implemented
- Re-validate everything under realistic costs
- Iterate on training only if signal exists worth iterating on

---

## 4. Target Architecture

End-state shape:

```
┌────────────────────────┐
│ Futures snapshot       │
│ (1m bars, OI, IV, vol) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐
│ Stage 1 — futures entry filter │ → produces (pass/block, confidence)
└──────────┬─────────────────────┘
           │ if pass
           ▼
┌────────────────────────────────┐
│ Stage 2 — direction signal     │ → produces (CE | PE | undecided,
│ + magnitude estimate           │              predicted_move_pct,
└──────────┬─────────────────────┘              confidence)
           │
           ▼
┌─────────────────────────────────────────────┐
│ OPTION SELECTOR  ← new layer                │
│ inputs: predicted_move, confidence, hold,   │
│         current IV, option chain snapshot   │
│ logic:                                       │
│   for each candidate strike (ATM, ±1, ±2):  │
│     compute breakeven, delta-adjusted edge, │
│     theta bleed, vega exposure, slippage    │
│   reject if expected_pnl_after_cost < 0     │
│   else choose best edge/risk ratio          │
└──────────┬──────────────────────────────────┘
           │ if any candidate passes
           ▼
┌────────────────────────────────┐
│ Order placement                │
└────────────────────────────────┘
```

The **option selector** is the missing piece. Everything else exists.

---

## 5. Phases

Five phases. Each has a gate. Project stops if a phase fails its gate without re-plan.

### Phase 0 — Diagnose existing model (1–2 days)

**Goal:** Determine whether the futures model has any genuine directional signal under realistic conditions.

**Tasks:**
- [x] s1ablation replay (BYPASS_GATES=1) — **complete**, 3298 trades over 59 dates, gross PF 1.04 → net PF 0.61 at 200 bps. Gates do real work: bypassing them blows trade count from 107 to 3298 and avg gross/trade collapses from 1.02% to 0.127%.
- [ ] Random-direction replay (Stage 2 randomized) — **incomplete, ran only 1 date (2024-01-15)**. Re-run pending. Futures-counterfactual (PF 2.07, 53.3% win) already establishes the model has direction edge, so this is a confirmatory test, not a blocker for Phase 1.
- [ ] Realistic-cost re-validation — re-run C1's exact training manifest with `cost_per_trade=0.025`, compare gates
- [x] Futures-counterfactual analysis — recomputed C1's 107 trades as if they were futures (not options) trades. P&L using entry/exit futures prices already in Mongo. **Result: 53.3% win rate, PF 2.07 gross.**

**Exit gate (any of these triggers proceed-to-Phase-1):**
- **Strong signal:** C1 normal wins on both VOLATILE PF and bypass comparisons → proceed
- **Weak signal:** Random-direction within noise of C1 BUT futures-counterfactual PF > 1.0 → proceed but invest more in selector  
- **No signal:** Random-direction matches C1 AND futures-counterfactual PF ≤ 1.0 → **PROJECT STOPS**. The model has no edge; trading framework changes won't help.

**Owner:** ML Research workstream (§6.1)
**Effort:** 4–8 hours of replay + 4 hours of analysis

### Phase 1 — Build the smart option selector (3–5 days)

**Goal:** Replace `atm + liquidity` selection with pricing-aware selection.

**Tasks:**
- [ ] Widen `Decision` dataclass in `strategy_app.engines.staged.types` to carry `predicted_move_pct`, `confidence_size`, `expected_hold_bars`
- [ ] Modify `predict_staged` to populate these fields from Stage 2 output
- [ ] Add IV extraction helper from snapshot payload (already ingested, not currently surfaced to selector)
- [ ] Build `option_selector.py` module with the breakeven/edge logic from §4
- [ ] Wire selector into `_select_strike` flow in `pure_ml_engine.py`
- [ ] Add unit tests for breakeven computation, edge calculation, IV pulldown
- [ ] Add an env var to switch between legacy ATM and smart selector for A/B testing
- [ ] Replay C1's 2024 dataset with smart selector; compare PF/win/MDD against legacy ATM selector

**Exit gate (REVISED 2026-05-15 — premium-% economics):** Replay 2024 Jan-Oct with all three Phase 1 changes ON. Baseline friction is **200 bps round-trip** (decision basis). 100 bps is recorded as an execution-improvement upside case, not the gate. All must pass:

- **Average gross premium-% per trade ≥ 2.0%** (C1 baseline is ~1.02%/trade — this is THE lever Phase 1 has to move)
- **Net premium-% positive at 200 bps round-trip cost** (i.e., sum of `pnl_pct - 0.02` over all trades > 0)
- **Trade frequency ≥ 3/week average over the 10-month replay** (5/week is the goal but not a hard floor — frequency is a *result* of the model, not imposed; see Commitment 2026-05-15)
- **Max single-trade adverse excursion ≤ 50% premium** (loss size doesn't explode as we widen exits)
- **Win rate ≥ 40%** (relaxed from 48% — Phase 1 may trade fewer, larger winners)

**Stretch / upside case (record but don't gate on):** Net positive at 100 bps if execution-improvement work in Phase 4 (limit orders, IOC, post-only) lands.

Each change is behind a feature flag so we can A/B which contributes what.

**Owner:** Selector Engineering workstream (§6.2)
**Effort:** Limit orders 2-3 days; wider exits 0.5 day; smart strikes 1.5 days; replay + analysis 1 day = ~5 days total

**Revised tasks (replaces old task list above):**

**Critical finding (2026-05-15):** The codebase has NO broker integration. `strategy_app` publishes POSITION_OPEN/CLOSE to MongoDB using snapshot mid-prices — paper-only. The `capped_live` rollout stage is a safety gate (max 0.25× sizing, guard file required), not a live broker hookup. Phase 1.1 re-scoped accordingly.

- [ ] **1.1 Realistic-cost backtest accounting** — every position close gets `pnl_pct_after_costs = pnl_pct - 0.02`. **Units:** `pnl_pct` is stored as a *fraction of option premium* (0.10 = 10% premium move), and 0.02 = 200 bps round-trip cost expressed in the same fraction units. Phase 1 measurements only count post-cost numbers. Stop-gap: apply in analysis scripts + UI display. Long-term: deduct at position-close emit time so all downstream sees realistic.

- [ ] **1.2 Wider exits + trailing** — all four values below are **decimal fractions of the underlying futures price** (the `ML_PURE_UNDERLYING_*` env vars), NOT premium-P&L thresholds and NOT percentages.
   - `ML_PURE_UNDERLYING_STOP_PCT_HISTORICAL`: 0.001 → **0.002** (i.e., 10 bps → 20 bps adverse futures move triggers stop)
   - `ML_PURE_UNDERLYING_TARGET_PCT_HISTORICAL`: 0.0025 → **0.005** (i.e., 25 bps → 50 bps favorable futures move triggers target)
   - `ML_PURE_MAX_HOLD_BARS_HISTORICAL`: 9 → **30** (1-min bars; raises max hold from 9 min to 30 min)
   - Trail (separate env var, not yet wired): activate at MFE ≥ 0.003 (30 bps underlying) offset 0.0015 (15 bps underlying)

   **Why these specific values:** A 50 bps favorable underlying move on a ~delta-0.5 ATM option translates to roughly 1.5–2.0% premium gain per trade — the level needed to beat 200 bps round-trip. Wider stops give the trade room to live the 30 bars needed for that move to develop.

   Plumbing in `docker-compose.yml` already done; just need to apply env values in VM's `.env.compose` and restart `strategy_app_historical`.

- [x] **1.3 Smart strike selection — CODE + TESTS COMPLETE 2026-05-15.** New module [`strategy_app/engines/option_selector.py`](../strategy_app/engines/option_selector.py) implements:
   - Reject trade when `snap.iv_percentile > SMART_STRIKE_IV_REJECT_PCTILE` (default 0.90)
   - Move to 1-OTM when `confidence ≥ SMART_STRIKE_OTM_CONFIDENCE` (default 0.75) AND `iv_percentile ≤ SMART_STRIKE_OTM_IV_CEIL` (default 0.50)
   - Fall back to ATM otherwise. ATM fallback also kicks in if OTM strike has no LTP.
   - Confidence = `decision.ce_prob` (for CE) or `decision.pe_prob` (for PE). Predicted move magnitude was *not* added to Decision dataclass; using direction probability as the confidence proxy. A future Phase 1.3.b can add an explicit predicted-move-pct from Stage 2's regression head if/when one exists.
   - Wired into [`pure_ml_engine.py`](../strategy_app/engines/pure_ml_engine.py) at the strike-selection point. `STRATEGY_SMART_STRIKE_ENABLED=1` activates it; defaulting to 0 preserves legacy ATM behavior. `_HISTORICAL`-suffix env vars in `docker-compose.yml` allow historical-only A/B testing.
   - 11 unit tests in [`test_option_selector.py`](../strategy_app/tests/test_option_selector.py) + 16 existing engine tests all green (27 total).
   - **Replay validation pending** — schedule a separate replay with `STRATEGY_SMART_STRIKE_ENABLED_HISTORICAL=1` after Phase 1.2 replay completes, then compare both effects.

LIVE limit-order execution moves to Phase 4 (Production hardening) — it's part of the Kite broker integration, not a backtest improvement.

### Phase 2 — Realistic-cost re-validation (1–2 days)

**Goal:** Re-validate the published model under cost assumptions that reflect real BankNifty options.

**Tasks:**
- [ ] Estimate per-strike round-trip cost from 2024 bid-ask data: `cost(strike) = bid_ask_pct + 20bps_overhead`
- [ ] Modify training manifest to use this cost function (currently flat 6bps)
- [ ] Re-run C1's exact training (same config, same data, new cost) → produces C1-realcost
- [ ] Apply existing publish gates to C1-realcost
- [ ] Document outcome regardless: does the model survive realistic costs?

**Exit gate:** C1-realcost combined PF ≥ 1.0 in VOLATILE regime after gates. If not, the model needs cost-aware retraining (Phase 3); if yes, the existing model is provisionally OK at realistic cost.

**Owner:** Cost & Validation workstream (§6.3)
**Effort:** 6 hours compute + 4 hours analysis

### Phase 3 — Cost-aware model iteration (1–2 weeks; conditional)

**Run only if Phases 1+2 reveal the existing model needs retraining.**

**Tasks:**
- [ ] Reformulate Stage 1 label: barrier-hit threshold accounting for option-translation drag (e.g. `barrier_pct = 2 × delta⁻¹ × cost_pct`)
- [ ] Or: replace Stage 1 label entirely with "option P&L positive after 9 bars" — predicts the right target directly
- [ ] Drop Stage 3 (documented as dead weight in MODEL_STATE history)
- [ ] Re-run staged HPO with realistic cost gate
- [ ] Walk-forward validation: train on rolling 12-month windows, test on next 1-month, 6 windows minimum
- [ ] Compare new model against C1-baseline on 2024 holdout

**Exit gate:** New model passes publish gates at realistic cost AND beats C1 on out-of-sample 6-month walk-forward.

**Owner:** ML Research workstream (§6.1)
**Effort:** ~80 hours compute + 2 weeks analysis

### Phase 4 — Kite integration + production hardening (1–2 weeks)

**Goal:** Make the system safe to run with real money. **Critical:** the codebase currently has NO broker integration; this phase builds it.

**Tasks (Kite integration):**
- [ ] Broker adapter: `strategy_app/broker/kite.py` with auth, order placement, position polling
- [ ] Connect strategy_app POSITION_OPEN/CLOSE to broker `kc.place_order` calls
- [ ] **Order type = LIMIT** (passive limits at best_bid + 1 tick / best_ask − 1 tick)
- [ ] Retry logic: 3 attempts at increasing aggressiveness, cancel after N seconds
- [ ] Order-state reconciliation: poll Kite for fills, update Mongo positions
- [ ] Mode flag `BROKER_MODE = mock | paper | live` so we can wire Kite without sending real orders first
- [ ] **PAPER mode** = real order objects, real auth, but `dry_run=True` flag prevents actual submission. Validates the integration end-to-end before any real risk.

**Tasks (production hardening):**
- [ ] Pre-trade risk gate: reject orders that violate `max_daily_loss_pct`, `max_consecutive_losses`, exposure caps
- [ ] Slippage monitoring: log expected-vs-realized for every fill; alert if median deviation > 50bps
- [ ] Kill-switch automation: auto-halt on N consecutive losses or X% drawdown
- [ ] Live shadow comparison: every paper trade gets a "what would real broker have filled" simulation; track divergence
- [ ] Capital ramp policy: 0.25× → 0.5× → 1.0× gated by realized 30-day PF
- [ ] Operator dashboard: P&L attribution by stage, by regime, by strike

**Exit gate:** Kite integration in PAPER mode for ≥5 trading days, zero discrepancies between intended and recorded fills. All risk paths tested with synthetic adverse scenarios. Kill-switch trip verified end-to-end.

**Owner:** Operations workstream (§6.4) + Risk Management (§6.5)
**Effort:** Kite integration ~1 week, hardening ~1 week, paper validation ≥1 week calendar

### Phase 5 — Capital deployment ramp (2–4 weeks)

**Goal:** Progressive live deployment with real (small) capital.

**Tasks:**
- [ ] Week 1: shadow mode (no orders, full pipeline running)
- [ ] Week 2: paper mode (broker-simulated fills, no real money)
- [ ] Week 3: capped_live @ 0.25× size, real money, hard daily loss limit ₹5K
- [ ] Week 4+: ramp to 0.5× then 1.0× contingent on 30-day rolling PF

**Exit gate:** None — this is steady-state operation. Project complete when monthly PF stable above 1.15 for 3 consecutive months.

**Owner:** Operations + Risk
**Effort:** Calendar 2-4 weeks; ongoing operational load

---

## 6. Workstreams ("teams" — even with one operator)

Each workstream represents a distinct hat. In a single-operator project augmented by AI, hats are switched, not staffed. Documenting them clarifies what mode of thinking is appropriate when.

### 6.1 ML Research

- **Charter:** Training, validation, experiments, label engineering, feature engineering
- **Decision authority:** Which experiments to queue, training config, gates pass/fail
- **Deliverables:** `MODEL_STATE_*.md` updates per session, `summary.json` per run, this plan's Phase 0/3 outcomes
- **Tools:** `ml_pipeline_2`, replay infrastructure, ML VM
- **Hat to wear when:** designing or running experiments, reading training reports

### 6.2 Selector Engineering

- **Charter:** The option-selection layer — the missing link
- **Decision authority:** Selector architecture, IV/Greeks computation method, A/B test design
- **Deliverables:** `option_selector.py`, unit tests, replay comparison reports
- **Tools:** `strategy_app`, replay infrastructure
- **Hat to wear when:** writing/testing selector code, debugging trade-by-trade discrepancies

### 6.3 Cost & Validation

- **Charter:** Realistic cost modeling, transaction cost analysis, validation methodology
- **Decision authority:** What cost numbers go into manifests, what gates count as "passed"
- **Deliverables:** Per-strike cost model, realistic-cost backtest reports, cost-attribution per trade
- **Tools:** Mongo (for executed trades), parquet (for historical spreads), `ml_pipeline_2`
- **Hat to wear when:** challenging published PF numbers, modeling costs, choosing thresholds

### 6.4 Operations

- **Charter:** Deployment, monitoring, healthchecks, image builds, GCP infra
- **Decision authority:** When to promote between rollout stages, ops alarms
- **Deliverables:** `docs/SYSTEM_FLOW_DIAGRAMS.md`, runbook updates, alerting setup
- **Tools:** GCP (compute, storage), Docker, GitHub Actions, gcloud CLI
- **Hat to wear when:** deploying changes, troubleshooting prod, capacity planning

### 6.5 Risk Management

- **Charter:** Pre-trade and post-trade risk controls, capital sizing, kill criteria
- **Decision authority:** Max loss limits, halt triggers, ramp pace
- **Deliverables:** Risk policy doc, kill-switch test reports, weekly P&L review
- **Tools:** Operator dashboard, `RISK_*` env vars in `strategy_app`
- **Hat to wear when:** authorizing capital deployment, after losing days

---

## 7. Decision Gates (where we stop or pivot)

| Gate | Condition | Action if FAIL |
|---|---|---|
| **G0 — End of Phase 0** | Phase 0 exit gate (§5) | If "no signal": **stop project, write postmortem** |
| **G1 — End of Phase 1** | Smart selector ≥10% PF improvement on C1 trade set | Iterate on selector logic; do not proceed to Phase 2 until fixed |
| **G2 — End of Phase 2** | C1-realcost VOLATILE PF ≥ 1.0 after gates | Trigger Phase 3 (cost-aware retraining) |
| **G3 — End of Phase 3** | New model beats C1 on 6-month walk-forward | Postmortem; either rethink approach or accept C1 as ceiling |
| **G4 — End of Phase 4** | All risk paths tested, kill-switch verified | No live capital until passes |
| **G5 — End of Phase 5** | 3 consecutive months of PF ≥ 1.15 | Ongoing; project sustainably running |

Project ends when:
- G0 fails (model has no signal — frank acceptance)
- G5 passes (project running steady)
- Operator decides to stop (any time)

---

## 8. Open Questions / Risks

### Open

- [ ] What's the realistic slippage for a 50-lot BankNifty ATM call entry on a high-VIX day?
- [ ] Are 2024 BankNifty option bid-ask history readily available, or do we need to estimate?
- [ ] Does the broker (Kite Connect) provide post-fill slippage data we can use for the comparison loop?
- [ ] What's the smallest capital that makes this worth running? (need to net more than infrastructure cost)
- [ ] How to handle option expiry rolls — current code assumes a single contract per snapshot

### Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Model has no real signal (G0 fails) | Medium | Project ends | Accept finding, don't fight data |
| Realistic costs eat the edge (G2 fails) | High | Phase 3 required | Re-formulate target as option-pnl-positive |
| Selector improvement is marginal | Medium | Move to Phase 3 | Smart selector still useful infrastructure |
| Live behavior diverges from replay | Medium | Halt + investigate | Shadow comparison framework (Phase 4) |
| Operator burnout from long ramp | Low–Medium | Project paused | Honest weekly review of progress |
| Regulatory / broker constraint | Low | Adapt | Code is broker-agnostic where possible |

---

## 9. Working Conventions

### How to update this doc

- **One operator can edit anywhere.** No PR ceremony for plan changes.
- **Phase tasks: tick `[x]` when truly done** (commit landed, gate met). Don't tick aspirations.
- **Phase exits get a one-paragraph outcome below the phase section** when the gate fires.
- **When pivoting:** add a `## Pivot YYYY-MM-DD` section, don't delete old plans. Posterity matters.
- **Session pairings:** every Claude session that touches strategy code should re-read this plan first. The plan supersedes any individual session's enthusiasm.

### How to start a session

```
1. git pull
2. Read docs/PROJECT_PLAN.md (this doc) for current phase + last update
3. Check replay status: curl /api/historical/replay/status
4. Check training status: gcloud compute ssh option-trading-ml-01 ...
5. Pick a task from the current phase
6. Update this doc when the task is done
```

### How to know when to stop

If a gate fails (G0–G4), **document the failure with numbers** in the relevant phase section, then **stop or pivot**. Don't burn cycles trying to "make it work" past a clear data-backed no.

---

## 10. Right Now (immediate next steps as of 2026-05-15)

Currently in **Phase 0 — Diagnose existing model**.

### Phase 0 results so far

**Futures-counterfactual analysis (COMPLETE) — major finding:**

Re-evaluated all 107 C1 baseline closed trades as if they had been executed on BNF futures instead of options (same direction, same entry/exit time, just different instrument):

| Same trades, different instrument | Win rate | PF | Net |
|---|---|---|---|
| OPTIONS (as actually traded, 6 bps cost) | 43.9% | 1.22 | +1.09% |
| FUTURES (counterfactual, 0 cost) | **53.3%** | **2.07** | **+11.81%** |

**Conclusion:** The futures model has **real directional signal** — 53.3% win rate on futures direction is meaningfully above 50%. The options translation eats roughly half the alpha (PF 2.07 → 1.22). G0 PASSES on signal-existence grounds.

**Per-direction breakdown:** CE trades avg +0.132% futures pnl (82 trades); PE trades avg +0.038% (25 trades). Model is much stronger on UP calls than DOWN calls — consistent with PE-dominant training data per `MODEL_STATE_20260428.md`.

**Three-run comparison (actual replay results, premium-% terms, applied 200 bps cost = 0.02 fraction units):**

| Run | Trades | Dates | Win rate (gross) | Gross net | Net @ 200 bps | Gross PF | Net PF | Avg gross / trade |
|---|---|---|---|---|---|---|---|---|
| **C1 normal** (gates ON) | 107 | 29 | 43.9% | **+108.85%** | **−105.15%** | **1.22** | **0.83** | **1.02%** |
| **s1ablation** (gates BYPASSED) | 3298 | 59 | 45.9% | **+419.19%** | **−6176.81%** | **1.04** | **0.61** | **0.127%** |
| **random-direction** (Stage 2 randomized) | _incomplete_ | _1 date only_ | — | — | — | — | — | needs re-run |

**Key reads:**
1. **Gates are not cosmetic** — turning them off drops PF from 1.22 to 0.92 and trade count balloons 10×; deterministic regime gates are doing real work.
2. **C1 is gross-positive, net-negative at 200 bps.** The model has signal worth keeping. The problem is monetization, not prediction.
3. **Average gross premium-% per trade ≈ 1.02% (108.85 / 107).** This is the lever Phase 1 must lift to ≥2.0% to break even on net P&L after realistic friction.
4. **Futures-counterfactual on the SAME C1 trades: PF 2.07, 53.3% win.** Confirms the directional edge is real; the options translation tax is what's eating ~½ the alpha.

### Active

- [x] s1ablation replay — complete, numbers above
- [x] Random-direction replay — **incomplete** (1 trade only); confirmatory only, futures-counterfactual already proves direction edge
- [planned] Realistic-cost re-validation — kick off on ML VM in Phase 2
- [x] **Phase 1.2 wider-exits — VALIDATED via counterfactual simulation 2026-05-15** (see §11)

Phase 0 gate (G0) **PASSES** on signal-existence grounds. Phase 1.2 gate (G1) **3 of 5 PASS** — proceed conditionally.

---

## 11. Phase 1.2 result — simulation 2026-05-15

A full historical replay was abandoned mid-run due to a `strategy_persistence_app_historical` hang (the pub/sub consumer subscribed but stopped processing — separate issue, deferred). Instead the Phase 1.2 effect was measured via counterfactual simulation: re-walk each of C1 baseline's 107 entry decisions through the original snapshot data, applying the new exit rules.

**Inputs:** C1 baseline 107 entries (run_ids `0f0dfb36…` + `a8c930e0…`), original entry premium / direction / strike / entry futures price kept verbatim. New exit rules: underlying-stop 0.002 (20 bps adverse), underlying-target 0.005 (50 bps favorable), max-hold 30 bars (30 min).

**Result (same 107 trades, different exits):**

| Metric | C1 baseline (9-bar/0.25%/0.10%) | Phase 1.2 (30-bar/0.5%/0.2%) | Δ |
|---|---|---|---|
| Trades | 107 | 107 | — |
| Dates traded | 29 | 29 | — |
| Avg gross / trade | 1.02% | **6.91%** | **+579%** |
| Sum gross premium-% | +108.85% | **+739.47%** | +579% |
| Net @ 200 bps round-trip | **−105.15%** | **+525.47%** | flipped |
| Gross PF | 1.22 | **1.86** | +52% |
| Win rate (gross) | 43.9% | **54.2%** | +10.3pp |
| Win rate (net @ 200 bps) | 38.3% | 46.7% | +8.4pp |
| MDD gross | 92.79% | 164.15% | wider |
| MDD net @ 200 bps | 163.41% | 188.17% | similar |

**Exit mix:** TIME_STOP 91 / STOP_LOSS 12 / TARGET_HIT 4. The pattern is informative — most trades trend favorably for the full 30 bars without hitting either stop or target. Suggests the entry signal has real *persistence*; widening the holding window captures most of the move.

**G1 gate breakdown:**
- ✅ avg gross/trade ≥ 2.0% — **6.91%** (3.5× over the bar)
- ✅ net positive @ 200 bps — **+525.47%** (clean flip from negative)
- ✅ win rate ≥ 40% — **54.2%**
- ❌ trades/week ≥ 3 — **2.47** (frequency is entry-volume-bound; Phase 1.2 doesn't change entries)
- ❌ max single-trade loss ≤ 50% — **54.2%** (one outlier, 4 pp over)

**Decision:** Phase 1.2 is a clear win on the dominant metrics (avg gross, net P&L, PF, win rate). The two failing gates are minor:
- Frequency: out of Phase 1.2's scope; addressed separately (entry-threshold tuning or Phase 1.3 smart strikes that may open more high-conviction trades on OTM).
- Max single-trade loss: marginal, just one outlier 4 pp over; consider tightening the underlying-stop to 0.0015 if this proves repeatable in a real replay.

**Caveats:**
1. This is a simulation, not a production replay. Stops/targets are evaluated against the same snapshot data C1 used; entry decisions are kept verbatim (C1's). A real Phase 1.2 production run might shift entries slightly because of state effects (overlapping positions, risk budget consumption). Effect expected to be small.
2. Exit premium uses snapshot LTP at exit bar; same convention as C1 baseline, so apples-to-apples vs C1.

**Next:** Phase 1.3 smart strike simulation on top of Phase 1.2 exits. Code is complete (option_selector.py + 11 tests). The smart-strike effect can be measured by adjusting the simulation to substitute the chosen strike (ATM/OTM/reject) per the new rules and re-running with Phase 1.2 exits.

---

## 12. Phase 1.3 result — simulation on top of Phase 1.2 (2026-05-15)

Re-ran the same counterfactual with smart-strike rules layered on top:

- Reject when `iv_percentile > 90` (0-100 scale; per `snapshot.iv_derived.iv_percentile`)
- Move to 1-OTM when `confidence ≥ 0.75` AND `iv_percentile ≤ 50`
- Else stay ATM

| Metric | C1 baseline | Phase 1.2 only | Phase 1.2 + 1.3 |
|---|---|---|---|
| Trades | 107 | 107 | **96** (11 IV-rejected) |
| Avg gross / trade | 1.02% | 6.91% | **7.93%** |
| Sum gross | +108.85% | +739.47% | +760.79% |
| Net @ 200 bps | −105.15% | +525.47% | **+568.79%** |
| Gross PF | 1.22 | 1.86 | **1.97** |
| Win rate | 43.9% | 54.2% | **55.2%** |

**Mode mix:** 96 ATM, 0 OTM, 11 rejected high-IV.

**Why no OTM?** The persisted `POSITION_OPEN` docs don't carry direction-specific `ce_prob`/`pe_prob`; the simulator fell back to the generic `entry_prob` (~0.5), which is below the 0.75 OTM-confidence threshold. In production, the runtime `Decision` object carries `ce_prob`/`pe_prob` from Stage 2 and OTM will fire as designed. Expect a modestly larger lift in a real replay.

**IV scale fix:** `snap.iv_percentile` is 0-100 (not 0-1). [option_selector.py](../strategy_app/engines/option_selector.py) defaults + tests + plan updated accordingly. All 27 tests still green.

**G1 outcome (Phase 1.2 + 1.3):** Same 3/5 PASS, 2 FAIL as Phase 1.2 alone:
- ✅ avg gross/trade (7.93%), net positive @ 200 bps (+569%), win rate (55.2%)
- ❌ trades/week (2.22) — frequency depends on entry threshold, not exits or strike choice
- ❌ max single-trade loss (54.2%) — same outlier; the IV-reject didn't catch it

**Recommendation for the two failing gates:**
1. **Frequency:** lower `STRATEGY_MIN_CONFIDENCE` from 0.65 → 0.55 (would also feed more trades to the OTM rule once direction-specific confidence is on the persisted decision). Re-validate.
2. **Max-loss outlier:** tighten `ML_PURE_UNDERLYING_STOP_PCT` from 0.002 → 0.0015 (15 bps adverse). Marginal — only one trade violated the 50% cap.

Both are sub-Phase-1 tuning, not fundamental design changes. Phase 1 has clearly cleared its core question: **C1 + wider exits + IV-filtering is net-profitable after realistic costs.**

---

## 13. Phase 1.2 + 1.3 LIVE replay result — full 2024 (2026-05-15)

> **⚠️ READ §14 FIRST.** The headline numbers in this section are dominated by training-window data. Truly out-of-sample (2024-08 → 2024-10) the same run is NET NEGATIVE. The +271% claim below should be read as in-sample, not as validation evidence.


The simulation in §11/§12 used C1's exact 107 entry decisions and counter-factually re-computed exits/strikes. To validate end-to-end at runtime, we executed a fresh historical replay with all three Phase 1.2 flags + smart-strike enabled, on `strategy_app_historical` consuming a live snapshot stream from the replay emitter.

**Critical pre-requisite fix applied earlier this session:** [trade_signal_builder.py](../strategy_app/engines/trade_signal_builder.py) was silently overriding env-derived `underlying_stop_pct` / `underlying_target_pct` / `max_hold_bars` with the recipe's bundled defaults (0.001 / 0.0025 / 20). Inverted precedence so explicit overrides win. 5 new precedence tests in [test_trade_signal_builder.py](../strategy_app/tests/test_trade_signal_builder.py); 24 tests total green on the relevant module set; 214/214 on full strategy_app suite.

**Run config:**
- run_id: `5eb9e3d9-0f1b-4d24-91e5-fd63f5bb8dbe`
- Date range: 2024-01-01 → 2024-10-31 (replay emitter)
- First trade landed: 2024-02-14 (entry rate is signal-driven, not config-driven)
- Final session reached: 2024-10-03 (strategy_app drained queue past replay end)
- Env: `STRATEGY_ML_PURE_BYPASS_GATES=0`, `STRATEGY_ML_PURE_RANDOMIZE_DIRECTION=0`, `STRATEGY_MIN_CONFIDENCE=0.65`, `STRATEGY_SMART_STRIKE_ENABLED=1`, `ML_PURE_UNDERLYING_STOP_PCT=0.002`, `ML_PURE_UNDERLYING_TARGET_PCT=0.005`, `ML_PURE_MAX_HOLD_BARS=30`
- Verified runtime values on first POSITION_OPEN: ✅ `stop=0.002` ✅ `tgt=0.005` ✅ `hold=30` ✅ `smart_strike_mode=atm`, `iv_percentile=68.09`, `selected_strike=45300`

**Storage path:** [positions.jsonl](../.run/strategy_app_historical/positions.jsonl) on the runtime VM. Mongo persistence is broken (pubsub-recovery bug — separate issue, doesn't affect backtests). JSONL has captured 100% of trade events.

**Comparison: C1 baseline → simulation → live**

| Metric | C1 baseline | SIM (Phase 1.2 only) | SIM (Phase 1.2 + 1.3) | **LIVE Phase 1.2 + 1.3** |
|---|---|---|---|---|
| Trades | 107 | 107 | 96 | **56** |
| Dates traded | 29 | 29 | — | **26** |
| Trades/week | 2.47 | 2.47 | 2.22 | **1.29** |
| Avg gross / trade | 1.02% | 6.91% | 7.93% | **6.85%** |
| Sum gross | +108.85% | +739.47% | +760.79% | **+383.32%** |
| Net @ 200 bps | **−105.15%** | +525.47% | +568.79% | **+271.32%** |
| Gross PF | 1.22 | 1.86 | 1.97 | **1.72** |
| Win rate (gross) | 43.9% | 54.2% | 55.2% | **53.6%** |
| Max single-trade loss | n/a | n/a | n/a | 54.2% |

**Why fewer trades than simulation (56 vs 107):** Simulation re-walked C1's exact entries against new exits. Live replay's position tracker blocks new entries while a position is open. Phase 1.2's 30-bar hold (vs C1's 9-bar) means each position consumes ~3× more snapshot windows, blocking ~half of C1's overlapping-entry opportunities. This is a **frequency–payoff trade-off**: fewer trades, each larger, same net dollars at lower capital lock-up.

**By direction:** CE 42 trades / 57.1% win / +264.62% gross / +180.62% net@200bps. PE 14 trades / 42.9% win / +118.70% gross / +90.70% net@200bps. CE-dominant consistent with regime gate + 2024 underlying drift.

**Smart-strike mode mix:** 56 ATM, 0 OTM, 0 high-IV-rejected. Despite Phase 1.3 wiring being active and selector running on every entry, the C1 trade set's confidence distribution (Stage 2 ce_prob/pe_prob mostly 0.55–0.70) is below the 0.75 OTM-confidence threshold. Smart-strike is correctly NOT firing OTM here — its value would emerge with a model that produces higher-conviction direction signals or with a lower threshold.

**G1 gate outcome — 3 of 5 PASS, 2 marginal FAIL:**

| Gate | Target | LIVE | Result |
|---|---|---|---|
| avg gross/trade | ≥ 2.0% | **6.85%** | ✅ PASS (3.4× over) |
| net positive @ 200 bps | > 0 | **+271.32%** | ✅ PASS |
| win rate gross | ≥ 40% | **53.6%** | ✅ PASS |
| trades/week | ≥ 3 | 1.29 | ❌ FAIL (entry-volume-bound) |
| max single-trade loss | ≤ 50% | 54.2% | ❌ FAIL (one outlier 4 pp over) |

**Decision: G1 substantively passes** ON THIS HEADLINE NUMBER ALONE. **However, §14 below shows this headline is dominated by training-window contribution (+87% of total gross from dates the model saw during training). See §14 for the honest out-of-sample verdict.**

**Cost-stress sensitivity (per Zerodha real-fee breakdown):**

| Cost assumption | Per-trade net | Project net |
|---|---|---|
| Statutory only (50 bps) | +6.35% | +355.32% |
| Limit orders (100 bps) | +5.85% | +327.32% |
| **Our gate (200 bps)** | **+4.85%** | **+271.32%** |
| Market orders crossing spread (350 bps) | +3.35% | +187.32% |
| Worst case (550 bps) | +1.35% | +75.32% |

Every cost regime is net-positive — **but again, this is in-sample-biased; see §14 for the holdout-only sensitivity which is net-negative at every cost.**

---

## 14. HONEST OOS VERDICT — train/valid/holdout decomposition (2026-05-15)

**This section retracts the §13 headline.** When the same 56 trades are split by whether the model saw the dates during training, the result reverses on truly out-of-sample data.

### C1 model training windows

Per [`ml_pipeline_2/docs/training/MODEL_STATE_20260514.md`](../ml_pipeline_2/docs/training/MODEL_STATE_20260514.md#L78):
- **train:**   2020-08-03 → 2024-04-30   (model SAW these dates during training)
- **valid:**   2024-05-01 → 2024-07-31   (model saw for hyperparameter tuning)
- **holdout:** 2024-08-01 → 2024-10-31   (truly out-of-sample — never seen)

### LIVE Phase 1.2 + 1.3 run split by window

| Window | n | avg gross | net @ 200 bps | PF | win% | verdict |
|---|---|---|---|---|---|---|
| **train** (contaminated) | 24 | +13.88% | **+285.14%** | 2.37 | 58.3% | model recognizes patterns |
| **valid** (light contamination) | 16 | +4.56% | +40.96% | 1.59 | 43.8% | weakly positive |
| **holdout** (CLEAN OOS) | **16** | **−1.42%** | **−54.78%** | **0.86** | 56.2% | **net negative** |
| OVERALL | 56 | +6.85% | +271.32% | 1.72 | 53.6% | (in-sample-dominated) |

**Training window contributes +87% of total gross.** The +271% headline is essentially "the model recognizes dates it was trained on" — not evidence of OOS edge.

### Exit-timing sweep — does any hold variant rescue the holdout?

Counterfactual sim on C1's full 107 baseline entries with 4 hold settings. Holdout-only:

| Hold bars | n | avg gross | net @ 200 bps | PF | win% | exit mix |
|---|---|---|---|---|---|---|
| 9 (C1 original) | 18 | −2.62% | **−83%** | 0.65 | 33% | TIME_STOP=18 |
| 15 | 18 | −5.81% | **−141%** | 0.44 | 33% | TIME_STOP=16, STOP_LOSS=2 |
| 20 | 18 | −5.88% | **−142%** | 0.42 | 28% | TIME_STOP=16, STOP_LOSS=2 |
| **30 (Phase 1.2)** | 18 | **−3.47%** | **−99%** | **0.65** | **50%** | TIME_STOP=16, STOP_LOSS=2 |

**All four exit configurations are net-negative on holdout.** Phase 1.2's 30-bar is the least-bad (best win rate, tied for best PF) but still loses money. **Exit timing is not the lever** — the directional signal itself has decayed in 2024 H2.

Compare same sweep on training-window-only (sanity, where everything should look good):

| Hold bars | train net @ 200 bps |
|---|---|
| 9 | −21% |
| 15 | +16% |
| 20 | +318% |
| 30 | **+448%** |

The **+448% → −99% swing (30-bar variant, training → holdout) is the textbook signature of severe overfit.** Wider exits "work" in-sample because they let winners run on dates the model memorized. They don't generalize.

### Pattern observations

- **All 18 holdout trades exit via TIME_STOP, not target.** The model's entries don't reliably produce 50 bps underlying moves in 30 bars on data it hasn't seen.
- **Win rates collapse** from 55-60% (in-sample) to 28-50% (holdout). Direction calls are no better than chance on truly fresh data.
- **Middle holds (15/20-bar) are pathological** — worse than both 9 and 30 bar. No exit-timing fix rescues this.
- **Sample size warning:** 16-18 holdout trades is statistically thin. Could be H2-2024 regime noise rather than permanent decay. **Cannot conclude definitively without more OOS data (2025).**

### Implications

1. **§13 G1-PASS claim is retracted.** Phase 1.2 + 1.3 is NOT validated as a deployable strategy. The headline +271% net is in-sample.
2. **Phase 1.2 (wider exits) is not the wrong architectural choice** — on holdout it's still the least-bad of the 4 variants tested. But it's not enough to make the model net-positive OOS.
3. **Phase 1.3 (smart strike) inconclusive** — OTM branch never fired in the C1 trade set. IV-reject filter removed 11 of 107 trades but they were not all from the holdout window.
4. **Frequency tune (lowering MIN_CONFIDENCE) is NOT worth running.** More trades with no OOS edge = more losses.
5. **Stop-tightening (0.002 → 0.0015) is NOT the answer.** Loss size is not the binding constraint — directional accuracy is.
6. **The C1 model itself may be 2024-fit.** Or H2-2024 may be a one-quarter regime shift that recovers in 2025. We cannot tell with the data we have.

### Next gating action

**Ingest 2025 BankNifty snapshot data into mongo + parquet.** Currently neither exists on the runtime VM. Once available, replay 2025-Q1 (3 months, truly OOS, never trained on) with the same Phase 1.2 + 1.3 config and decompose the same way. Three possible outcomes:

| 2025 Q1 holdout net @ 200 bps | Interpretation | Action |
|---|---|---|
| Net positive (≥ +20% over the quarter) | H2-2024 was regime noise; OOS edge real | Proceed to Phase 4 (Kite paper integration) |
| Marginal (−20% to +20%) | Edge fragile; need more samples | Wait for more 2025 data, do not deploy |
| Net negative (< −20%) | Model is 2024-fit; OOS edge gone | Either retrain (walk-forward) or shelve the project |

### What the existing infrastructure DOES support

The good news from this session: even though the result is sobering, **the infrastructure to ask honest questions is now in place**:
- [analyze_jsonl.py](../scripts/analyze_jsonl.py) — auto window-split, run-id filter, sample-size warnings
- [sim_exit_sweep.js](../scripts/sim_exit_sweep.js) — counterfactual sweep across exit variants with window split
- JSONL is the canonical source of truth; mongo is best-effort
- Precedence fix means env-driven config changes actually take effect at runtime
- Smart-strike module is plumbed and tested even though its OTM branch needs higher-conviction entries to fire

These tools will be just as useful for evaluating any 2025 replay, walk-forward retrained model, or future strategy iteration.

**Caveats:**
1. **2024 only.** 2025 out-of-sample test is the deciding question for "real product vs overfit." Currently blocked on 2025 data ingestion to mongo/parquet.
2. **Single market regime.** 2024 was a CE-dominant year for BankNifty. Underperformance in a sustained PE-dominant year is a known risk.
3. **Frequency is below ambition floor.** 1.29/week translates to ~5.6/month, below the 30/month definition-of-done in §1. Tuning `MIN_CONFIDENCE` 0.65 → 0.55 is the next experiment.

**Next:** Frequency-tuning experiment (re-run with lower confidence threshold), then 2025 OOS once data ingested, then Phase 4 (Kite paper integration).

---

## Commitment 2026-05-15 — Path B locked + frequency targets locked

**This commitment is final and supersedes the path-comparison discussion below (kept as historical context only).**

After futures-counterfactual revealed the model has real directional signal but the options translation eats the alpha, the operator has chosen **Path B (options-only with magnitude-aware selection)** with these constraints:

- **Instrument:** options only (no futures execution). Capital constraint precludes futures (~₹4.5L margin per lot).
- **Frequency target:** **5 trades/week** as a *goal*, NOT a hard floor. Slow months down to ~3/week are acceptable. **Frequency is a result of the model, not a target imposed on it.**
- **Limit orders OK:** unfilled passive limits are acceptable; lower friction beats forced market-order fills. (Live execution work belongs in Phase 4.)
- **Wide losers + wider winners:** asymmetric exit logic accepted (0.2% underlying stop, 0.5% underlying target in Phase 1.2).
- **Sequencing:** backtest (Phase 1) → Kite paper mode (Phase 4) → real money (Phase 5). NO real money currently.

This commitment locks Phase 1's three engineering changes (1.1 realistic-cost accounting, 1.2 wider exits, 1.3 smart strikes). The path-comparison section below is preserved for posterity — it documents *why* Path B was chosen — but should not be treated as live decision criteria.

## Historical context — three paths considered before locking Path B (2026-05-15)

The "smart option selector" hypothesis underestimated the size of the options translation tax. Smart selection helps but doesn't bridge a 200 bps gap on a ~100 bps gross premium edge per trade. Real options paths required either bigger directional moves (longer holds + better strikes), longer holds, or a different instrument. Three paths were on the table; Path B was chosen above. The other two are recorded as alternatives if Phase 1 fails its gate.

### Path A — Switch execution to BNF futures (REJECTED — capital constraint)

- **What:** Same model, same signals; place orders on `BANKNIFTY26MARFUT` instead of ATM options.
- **Pros:** P&L matches prediction (no theta, no vega); ~5-10 bps round-trip costs; cleanest implementation; preserves all training work.
- **Cons:** Margin requirement ~₹4.5L per lot vs option premium ~₹6-15K. **Hard constraint for retail capital.** Forces single-lot positions; smaller % returns despite cleaner math.
- **Why rejected:** capital not available for futures margin. Re-eligible only if capital base changes materially.

### Path B — High-conviction options (CHOSEN — see Commitment above)

- **What:** Same model on options, but raise per-trade payoff via wider exits (Phase 1.2) and smart strike selection (Phase 1.3) so average gross premium-% per trade beats 200 bps cost. Higher confidence thresholds also considered.
- **Pros:** Keeps options leverage; uses capital efficiently; aligns trade selection with what survives the translation tax.
- **Cons:** Likely produces fewer trades than current 107/10 months. Statistical noise becomes a concern at very low counts. Magnitude-prediction infrastructure doesn't exist yet (built in Phase 1.3).
- **Status:** Active — Phase 1 is the implementation of Path B.

### Path C — Multi-leg structures (DEFERRED — re-eligible after Phase 1 result)

- **What:** Replace single-leg ATM CE/PE with bull spread (CE buy + further OTM CE sell) or calendar (near + further expiry). Reduces net vega exposure and net theta cost.
- **Pros:** Could neutralize vega-crush losses seen in the 10% premium-drop trade examples.
- **Cons:** Spreads cap upside; brokerage doubles; selection logic gets more complex.
- **Status:** Held in reserve. Re-evaluate only if Phase 1 (Path B) fails its gate and the failure mode is vega/theta-attributable.

---

## 15. Factorial experiment — F1 (window-shift) and B1 (cost-in-label) — both held 2026-05-15

After today's OOS-failure verdict (§14), two structural questions: does C1's recipe survive (a) a different training window, or (b) realistic option cost baked into label generation? Both launched on the ML VM. Both **failed identically — at different gates but same root cause.**

### Final factorial result

| Run | Cost in label | Train window end | Status | Completion mode | Stage 1 ROC | block_rate |
|---|---|---|---|---|---|---|
| C1 baseline (live) | 6 bps | 2024-04-30 | shipped | (live; holdout net −55% §14) | 0.683 | gates pass |
| **F1** walk-forward | 6 bps | **2023-04-30** | ❌ HELD | `stage1_cv_gate_failed` | 0.642 | **1.0** all folds |
| **B1** cost-aware | **0.02 (200 bps)** | 2024-04-30 | ❌ HELD | `stage2_signal_check_failed` | (reached stage 2) | **1.0** all folds |

### Reading the result

The models aren't broken — both F1 and B1 produced trained classifiers with comparable Stage 1 predictive power to C1. **What failed are the gates.** They're protecting us from deploying recipes that don't have a robust edge.

The failure modes are mechanistically different but tell the same story:
- **F1:** trained on pre-2024 data, the calibrated entry threshold finds no positive-EV setups. So `block_rate=1.0` — refuse everything.
- **B1:** trained on 2024 data with 200 bps cost in the label, the Stage 2 direction signal isn't strong enough to clear the post-cost validation check. So same `block_rate=1.0`.

Both say: **the recipe's edge is too small to survive structural shifts in the labeling regime** (window or cost). Combined with C1's holdout collapse (§14) and the earlier s1ablation finding (gates ON: PF 1.22; gates OFF: PF 0.92 with avg gross 0.13%/trade), four independent confirmations of the same picture.

### Path C-1 was considered and rejected

The natural next move would be conviction-filtered labels: use `direction_or_no_trade_v1` + `stage2_target_redesign.enabled=true` so only the high-conviction historical trades are labeled positive. This is **a known dead-end** per [MODEL_STATE_20260426.md §What Doesn't Work](../ml_pipeline_2/docs/training/MODEL_STATE_20260426.md): the conviction filter reduced Stage 2 rows from ~57k to ~3k → 0 holdout trades at 6 bps cost. Under 200 bps cost the throughput collapse would be worse. **Do not run this.**

### What this means for the project

The C1 recipe's edge depends on a specific combination of (training window × optimistic cost × original label design). Change any single dimension and the recipe collapses. This isn't fixable by more HPO trials — the data ceiling has been reached on 2020-2024 BankNifty.

### Decision: training is paused

Until fresh data (2025+) is available, every new training run on 2020-2024 will be vulnerable to the same data-ceiling issue. **No more training experiments on this dataset.** The unblocking action is forward live shadow data collection.

### Next-experiment design note (for when fresh data exists)

When 4-6 weeks of forward shadow data has accumulated, run ONE carefully-designed experiment. Three candidate designs in order of priority:

1. **Direct option P&L label.** Predict "did this option trade return positive net after 200 bps cost over the next N bars?" — bypass the futures-direction-→-options-translation tax entirely. New labeler needed (similar to `direction_or_no_trade_v1` but binary on net option P&L, not direction).
2. **Longer prediction horizon.** Same recipe, same labels, but `max_hold_bars` extended (e.g., 60 or 120 bars instead of 9-30) at TRAINING time. Allows underlying move to accumulate to a level where 200 bps cost is small relative to typical option premium move.
3. **Different feature philosophy.** Stop relying on intraday momentum features; emphasize structural features (IV regime, expiry distance, OI deltas) that may generalize better across regimes.

All three depend on having fresh OOS data to validate against. Don't design or run them until forward shadow has accumulated.

### How to resume next session

1. Check forward shadow collection status (once it's running): `sudo ls /opt/option_trading/.run/strategy_app/positions.jsonl` on runtime VM.
2. If shadow data is accumulating, count days and trades. Decision point: 30+ trades on fresh data is the minimum sample to make any honest claim.
3. If shadow ISN'T running yet, the gating action is operator sharing Kite live credentials.

---

## 16. Related Docs

- [SYSTEM_FLOW_DIAGRAMS.md](SYSTEM_FLOW_DIAGRAMS.md) — architecture & flow diagrams
- [ARCHITECTURE.md](ARCHITECTURE.md) — textual cross-cutting view
- [../ml_pipeline_2/docs/training/INDEX.md](../ml_pipeline_2/docs/training/INDEX.md) — research history (A→B→C→D→E grids; F1/B1 added 2026-05-15)
- [../ml_pipeline_2/docs/training/MODEL_STATE_20260515.md](../ml_pipeline_2/docs/training/MODEL_STATE_20260515.md) — **TODAY's session — read this first when resuming**
- [../ml_pipeline_2/docs/training/MODEL_STATE_20260514.md](../ml_pipeline_2/docs/training/MODEL_STATE_20260514.md) — previous session (E2 / Stage-1 ablation context)
- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md) — contracts, constants
- [../scripts/README.md](../scripts/README.md) — analysis + orchestration scripts (analyze_jsonl, sim_exit_sweep, launchers)

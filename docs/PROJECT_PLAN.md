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

## 2. Current State Snapshot (2026-05-15)

| Layer | State |
|---|---|
| **Live model** | C1 (`staged_deep_hpo_c1_base_20260429_040848`), `regime_gate_v1` active, `capped_live` rollout @ 0.25× size |
| **Live behavior** | ~14% of trading days trade. ~80 trades over 10 months of 2024 replay. Win rate 45.6%. Net P&L +1.09% (gross, 6bps cost assumption). |
| **Architecture** | Three lanes (training / live / historical replay) sharing same `strategy_app` code and published model artifact. Documented in [SYSTEM_FLOW_DIAGRAMS.md](SYSTEM_FLOW_DIAGRAMS.md). |
| **Training pipeline** | `ml_pipeline_2` staged HPO (S1 entry · S2 direction · S3 recipe). 5 grids run to date (A, B, C/C1, D/D2, E/E2). C1 force-deployed; D2 held; E2 failed across 5 gates. |
| **Active experiments** | s1ablation replay (BYPASS_GATES=1) — ~25 min remaining. Random-direction replay queued. |
| **Known gaps** | (1) futures→options selector is naive, (2) cost model is 30–60× optimistic, (3) ML→selector handoff drops magnitude info, (4) Stage 3 is dead weight, (5) no shadow-vs-paper-vs-live framework. |

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
- [x] s1ablation replay (BYPASS_GATES=1) — measures whether deterministic gates do real work
- [ ] Random-direction replay (Stage 2 randomized) — measures whether Stage 2 predicts better than chance
- [ ] Realistic-cost re-validation — re-run C1's exact training manifest with `cost_per_trade=0.025`, compare gates
- [ ] Futures-counterfactual analysis — recompute C1's 80 trades as if they were futures (not options) trades. P&L using entry/exit futures prices already in Mongo.

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

**Exit gate (REVISED 2026-05-15):** Replay 2024 Jan-Oct with all three changes ON. All must pass:
- Net P&L > 0 at realistic 75 bps round-trip cost
- Trade frequency ≥ 4/week average
- Max drawdown < 5% on portfolio  
- Win rate ≥ 48%

Each change is behind a feature flag so we can A/B which contributes what.

**Owner:** Selector Engineering workstream (§6.2)
**Effort:** Limit orders 2-3 days; wider exits 0.5 day; smart strikes 1.5 days; replay + analysis 1 day = ~5 days total

**Revised tasks (replaces old task list above):**

**Critical finding (2026-05-15):** The codebase has NO broker integration. `strategy_app` publishes POSITION_OPEN/CLOSE to MongoDB using snapshot mid-prices — paper-only. The `capped_live` rollout stage is a safety gate (max 0.25× sizing, guard file required), not a live broker hookup. Phase 1.1 re-scoped accordingly.

- [ ] **1.1 Realistic-cost backtest accounting** — every position close gets `pnl_pct_after_costs = pnl_pct - 0.02` (200 bps round-trip baseline). Phase 1 measurements only count post-cost numbers. Stop-gap: apply in analysis scripts + UI display. Long-term: deduct at position-close emit time so all downstream sees realistic.
- [ ] **1.2 Wider exits + trailing** — stop 0.001→0.002, target 0.0025→0.005, hold 9→30 bars, trail activate at MFE ≥ 0.3% offset 0.15%. (Plumbing already done in `docker-compose.yml`; just need to apply env values.)
- [ ] **1.3 Smart strike selection** — widen `Decision` to carry predicted move + confidence; compute IV percentile from rolling snapshots; reject when IV percentile > 0.9; switch to 1-OTM when confidence > 0.75 AND predicted move > 0.5%. Env: `STRATEGY_SMART_STRIKE_ENABLED=1`.

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

**Realistic-cost stress (rough estimates):**
- OPTIONS @ 200 bps: net −213% — completely unviable at C1's hold duration / trade frequency
- FUTURES @ 10 bps (BNF liquid mid-day): ~+1.1% net — borderline profitable
- FUTURES @ 5 bps (best case): ~+6.5% net — clearly profitable

**Implication for Phase 1:** The smart selector alone cannot rescue the options strategy at realistic costs. Three real paths emerged (see Pivot 2026-05-15 below).

### Active

- [running] s1ablation replay — ~10 min remaining
- [queued] Random-direction replay — auto-starts after s1ablation; **still informative** because it tests whether Stage 2 specifically has direction edge, separate from Stage 1's entry edge
- [planned] Realistic-cost re-validation — kick off on ML VM

When all four are done, evaluate full G0 + commit to a path from the three below.

---

## Commitment 2026-05-15 — Path B chosen + frequency targets locked

After futures-counterfactual revealed the model has real directional signal but options translation eats the alpha, the operator has chosen **Path B (options-only with magnitude-aware selection)** with these constraints:

- **Instrument:** options only (no futures execution). Capital constraint accepted.
- **Frequency target:** **5 trades/week** as a *goal*, NOT a hard floor. Slow months down to ~3/week are acceptable. **Frequency is a result of the model, not a target imposed on it.**
- **Limit orders OK:** unfilled passive limits are acceptable; lower friction beats forced market-order fills.
- **Wide losers + wider winners:** asymmetric exit logic accepted (0.4-0.5% stops on underlying).

This commitment locks Phase 1's three engineering changes (see below) and supersedes the old "smart selector" framing.

## Pivot 2026-05-15 — three real paths after futures-counterfactual

The "smart option selector" hypothesis underestimated the size of the options translation tax. Smart selection helps but doesn't bridge a 200 bps gap on a 11 bps gross edge. Real options paths require either bigger directional moves, longer holds, or different instrument.

### Path A — Switch execution to BNF futures

- **What:** Same model, same signals; place orders on `BANKNIFTY26MARFUT` instead of ATM options.
- **Pros:** P&L matches prediction (no theta, no vega); ~5-10 bps round-trip costs; cleanest implementation; preserves all training work.
- **Cons:** Margin requirement ~₹4.5L per lot vs option premium ~₹6-15K. **Hard constraint for retail capital.** Forces single-lot positions; smaller % returns despite cleaner math.
- **When this wins:** If capital allows ≥1 lot, this is the highest-EV path.

### Path B — High-conviction options (reduce frequency, increase predicted move size)

- **What:** Same model, but trade only when Stage 1 + Stage 2 confidence > 0.80 (currently 0.65). Estimate predicted move size from Stage 2 output (requires Decision dataclass widening per original Phase 1). Reject trades unless predicted move > 2× implied breakeven.
- **Pros:** Keeps options leverage; uses capital efficiently; aligns trade selection with what survives the translation tax.
- **Cons:** Likely produces 10-20 trades/year instead of 80-100. Statistical noise becomes significant. Requires the magnitude-prediction infrastructure that doesn't exist yet.
- **When this wins:** If capital is too small for futures and we accept low-frequency, high-conviction trading.

### Path C — Multi-leg structures (spread/calendar)

- **What:** Replace single-leg ATM CE/PE with bull spread (CE buy + further OTM CE sell) or calendar (near + further expiry). Reduces net vega exposure and net theta cost.
- **Pros:** Could neutralize vega-crush losses we see in the 10% premium-drop trade examples.
- **Cons:** Spreads cap upside; brokerage doubles; correctness of selection logic gets more complex.
- **When this wins:** If we want to keep options leverage but manage vega — and have the engineering bandwidth.

### Decision criteria

Before committing to a path, also need:
- Random-direction replay outcome (still pending) → tells us if Stage 2 specifically has direction edge, or if Stage 1 alone produces the 53.3% futures-win
- Operator capital constraint check (futures lot ≈ ₹4.5L; do we have it?)
- Operator risk-frequency preference (more trades vs more conviction)

Path A is mechanically simplest. Path B is most aligned with the original design intent. Path C is most engineering-heavy. **Default recommendation pending random-direction result: Path A if capital allows, else Path B.**

---

## 11. Related Docs

- [SYSTEM_FLOW_DIAGRAMS.md](SYSTEM_FLOW_DIAGRAMS.md) — architecture & flow diagrams
- [ARCHITECTURE.md](ARCHITECTURE.md) — textual cross-cutting view
- [../ml_pipeline_2/docs/training/INDEX.md](../ml_pipeline_2/docs/training/INDEX.md) — research history (A→B→C→D→E grids)
- [../ml_pipeline_2/docs/training/MODEL_STATE_20260514.md](../ml_pipeline_2/docs/training/MODEL_STATE_20260514.md) — last training session state
- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md) — contracts, constants

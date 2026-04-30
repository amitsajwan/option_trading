# Stage 2 Direction Model — Recovery Plan
> **Status**: Active  
> **Last updated**: 2026-04-12  
> **Owner**: ML team  
> **Context**: Stage 2 (CE/PE direction gate) has consistently failed to reach ROC ≥ 0.55 across 6 experiments. Stage 1 (entry gate) is solid at ROC 0.619. This document is the agreed plan forward.

---

## Current Situation

```
WHAT WORKS:
  Stage 1 (entry gate)   ROC 0.619   ✓  stable, proven, not the problem
  Stage 3 (recipe)       not blocked  ✓  recipe catalog + policy tested

WHAT DOESN'T WORK:
  Stage 2 (direction)    ROC ~0.52–0.54   borderline, high drift across regimes
  Drift metric:          0.13–0.19        model flips signal between time periods
  Experiments so far:    6 runs, 0 publishable
```

---

## Experiment History

| ID | Run | Stage 2 ROC | Drift | Outcome | Root cause |
|----|-----|-------------|-------|---------|------------|
| S0 | logreg, original features | ~0.52 | unknown | held | Baseline — regime follower |
| S1 | feature wrappers / thresholds | — | — | gate_failed | Threshold tricks don't add signal |
| S2 | 24-feature signal analysis | — | — | 0/24 stable | Confirmed regime amplifier problem |
| S3a | rolling oracle regime features (baseline) | 0.541 | 0.135 | gate_failed | Signal flips between halves |
| S3b | rolling oracle regime features (balanced) | 0.548 | 0.191 | gate_failed | Mirror flip of S3a |
| S4-screen | stage2_family_screen_campaign_v2 (running) | TBD | TBD | running | 16 lanes, results tomorrow |

---

## Root Cause (Confirmed)

Stage 2 is a **regime amplifier**, not a direction predictor:
- It learns the dominant CE/PE regime of its training window
- Projects that regime forward blindly into validation/holdout
- Works in windows where regime is stable, collapses when it shifts
- Oracle itself is only ~55% directional on MIDDAY sessions — this is the hard ceiling

**The problem is not the model. The problem is the target is near-random.**

---

## The Plan (Decided 2026-04-12)

### Principle
> Do not lower the publish gate (1.5 PF). Change the approach instead.  
> Ship CE-only on Stage 1 + Stage 3. Run direction search in parallel, not blocking.

---

## Track A — CE-Only Model (PRIMARY, start immediately after screen)

**Hypothesis**: Stage 1 at ROC 0.619 identifies high-probability trade windows. Stage 3 selects the best recipe. Removing the weak Stage 2 link improves overall system reliability.

**Why CE specifically**:
- BankNifty structural CE bias (market makers net long CE, retail directional bets)
- Stage 3 recipe catalog built around CE recipes (L0/L3/L6)
- More trades (no Stage 2 filter reduces trade count)

### Implementation

```
Step 1 — Add bypass_stage2 flag to pipeline.py          [1 day, code]
Step 2 — New CE-only manifest config                    [2 hrs, config]
Step 3 — Generate-only validation on GCP                [30 min]
Step 4 — Full GCP run (Stage 1 already trained)         [2 hrs compute]
Step 5 — Shadow deploy alongside current system         [1 day, infra]
```

### Success Criteria

```
Shadow phase (3 weeks live data):
  SHIP:   profit_factor ≥ 1.5, trades ≥ 50, drawdown ≤ 10%
  HOLD:   profit_factor ≥ 1.2 → continue shadow, do not ship
  STOP:   profit_factor < 1.0 → Stage 1 or Stage 3 is the problem → reframe

Deployment (if ship):
  Week 1–2: 25% position size
  Week 3–4: 50% if P&L positive
  Month 2+: full size if stable
```

### Risk Guardrails (live)
- CE side_share > 75% over rolling 20 days → auto-pause, human review
- Live drawdown hits 7% → auto-pause (gate is 10%, trip at 7% for buffer)

---

## Track B — Direction Model Enhancement (PARALLEL, not blocking)

**Hypothesis**: Direction is learnable but requires better features + model. Run in parallel with Track A, not instead of it.

### Step B1 — Screen Results Analysis (tomorrow, after campaign finishes)

```
When: 2026-04-13 ~18:00 UTC (screen completes)
What:
  - Pull all 192 run results
  - Rank by DRIFT (not ROC) — low drift = stable model
  - Find top-3 lanes: (drift < 0.08 AND ROC closest to 0.55)
  - Record: which feature family + session filter + model type won
```

### Step B2 — HPO + Calibration on Top-3 (1 GCP run, ~6 hrs)

```
For each of top-3 winning lanes:
  - Stage 2 hyperparameter sweep:
      logreg: C ∈ [0.01, 0.1, 1.0, 10.0]
      tree:   max_depth ∈ [3, 5, 7], learning_rate ∈ [0.05, 0.1, 0.3]
  - Platt scaling calibration (fixes Brier gate directly)
  - Extended oracle windows: 20d, 40d (current: 5d, 10d only)
  - Add untapped features:
      ce_volume_5m / pe_volume_5m ratio (options flow)
      yday_close_direction (previous day context)
      PRE_EXPIRY × direction interaction term

Gate to pass: ROC ≥ 0.55 AND drift < 0.08
If passes: ensemble with CE-only as base layer
If fails: archive, do not spend more compute
```

### Step B3 — Ensemble (if B2 passes)

```
Architecture:
  CE-only model        → base confidence
  Direction model      → direction modifier
  
  Final signal:
    IF direction_confidence > 0.60 → use direction model's CE/PE
    ELSE → default to CE-only
  
  This keeps CE-only as the floor while letting direction add alpha
  when it's confident.
```

---

## Track C — Live Inference Gap (BLOCKING for any fo_midday_direction_regime_v1 deploy)

⚠️ **Must resolve before shipping any model using rolling oracle features**

```
Problem:
  oracle_rolling_ce_win_rate_5d/10d are computed from historical oracle
  during training. For live ml_pure lane, these features need a
  daily pre-computed lookup table.

Work required:
  1. Daily job: compute rolling oracle stats from yesterday's trades
  2. Store as: oracle_rolling_stats_{date}.parquet
  3. ml_pure lane: load stats at inference time, join by trade_date
  4. Fallback: if stats missing → use 50/50 prior (neutral)

Estimate: 2–3 days, Data Engineering
Owner: Data Engineering
Blocks: any live deployment of fo_midday_direction_regime_v1
Does NOT block: CE-only track (no oracle features needed)
```

---

## Decision Gates

```
After screen results (2026-04-13):
  → Start CE-only implementation (Track A)
  → Identify top-3 lanes for B2

After CE-only shadow (3 weeks, ~2026-05-05):
  → If PF ≥ 1.5: ship CE-only
  → If PF < 1.0: both Stage 1 and Stage 3 need review (reframe)

After B2 HPO run (~2026-04-18):
  → If ROC ≥ 0.55 + drift < 0.08: ensemble with CE-only
  → If fails: close direction search, CE-only is the final model

Hard stop rule:
  If best model after Track A + Track B is PF < 1.1 on shadow:
  → Stage 2 direction is not learnable with current data/features
  → Pivot to undirected entry-only model
  → Document as closed research thread
```

---

## What We Are NOT Doing

```
✗ Lowering the publish gate to 1.3 PF
  Reason: slippage + edge decay means 1.3 backtest → 1.0–1.1 live

✗ Spending more GCP time on direction search before CE-only shadow
  Reason: CE-only gives live data signal in 3 weeks.
          That's worth more than another 16-lane historical backtest.

✗ Waiting for perfect Stage 2 before deploying anything
  Reason: Stage 1 + Stage 3 is a working system. Ship it.
```

---

## Monitoring the Current Run

**Campaign**: `stage2_family_screen_campaign_v2`  
**GCP artifact**: `/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/campaign_runs/stage2_family_screen_campaign_v2/`  
**Expected completion**: 2026-04-13 ~18:00 UTC (06:00 IST next day)  
**Nothing to do on GCP until it finishes.**

Quick status check command (run on GCP):
```bash
# Lane completion count
grep -c '"status": "running"\|"status": "held"\|"status": "gate_failed"\|"status": "publishable"' \
  ~/option_trading/ml_pipeline_2/artifacts/campaign_runs/stage2_family_screen_campaign_v2/workflow_state.json

# Current running lane's progress
grep status ~/option_trading/ml_pipeline_2/artifacts/campaign_runs/stage2_family_screen_campaign_v2/lanes/*/runner_output/runs/*/run_status.json | grep running
```

---

## Next Actions (Priority Order)

| # | Action | Owner | When | Effort |
|---|--------|-------|------|--------|
| 1 | Wait for screen to finish | GCP (automatic) | 2026-04-13 18:00 UTC | — |
| 2 | Analyse screen: rank by drift, pick top-3 | ML | After screen | 2 hrs |
| 3 | Implement bypass_stage2 CE-only flag | Engineering | 2026-04-14 | 1 day |
| 4 | CE-only GCP run + shadow deploy | ML + Infra | 2026-04-15 | 1 day |
| 5 | Resolve oracle_rolling live inference gap | Data Engineering | 2026-04-14 | 2–3 days |
| 6 | HPO + calibration on top-3 direction lanes | ML | 2026-04-16 | 1 day config + 6 hrs GCP |
| 7 | Review shadow results | All | 2026-05-05 | Meeting |

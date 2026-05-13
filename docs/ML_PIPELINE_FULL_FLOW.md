# ML Pipeline — Full Flow & Case Study

> **Purpose**: Reference document for the BankNifty Futures intraday options trading ML pipeline.
> Covers data → training → campaign orchestration → publish decision.
> Last updated: 2026-05-02

---

## 1. The Big Picture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        RAW MARKET DATA                                  │
│   BankNifty Futures snapshots · H1:30 midday window · 2020–2024        │
│   ~970 trading days · ~15,000 midday snapshots                         │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │  Parquet files
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     FEATURE ENGINEERING                                  │
│   fo_full · fo_expiry_aware_v2/v3 · fo_oi_pcr_momentum                 │
│   fo_iv_skew_only · fo_no_opening_range · fo_no_time_context            │
│   fo_midday_direction_regime_v1  ← NEW (rolling oracle win-rate)        │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                    ┌───────────┴───────────┐
                    │                       │
                    ▼                       ▼
           ┌──────────────┐       ┌──────────────────┐
           │   STAGE 1    │       │    ORACLE BUILD   │
           │  Entry Gate  │       │  Best recipe per  │
           │  (All rows)  │       │  snapshot scored  │
           └──────┬───────┘       └──────────────────┘
                  │ YES rows only          │
                  ▼                        │ labels + rolling stats
           ┌──────────────┐               │
           │   STAGE 2    │◄──────────────┘
           │ Direction    │
           │  CE / PE     │
           └──────┬───────┘
                  │ CE or PE rows only
                  ▼
           ┌──────────────┐
           │   STAGE 3    │
           │ Recipe Select│
           │ L0 / L3 / L6 │
           └──────┬───────┘
                  │
                  ▼
           ┌──────────────┐
           │  ECONOMICS   │
           │  GATE CHECK  │
           └──────┬───────┘
                  │
         ┌────────┴────────┐
         ▼                 ▼
   PUBLISHABLE           HELD
   (deploy to live)   (logged, not deployed)
```

---

## 2. What Each Stage Does

### Stage 1 — Entry Gate
> **Question**: "Is this midday snapshot worth trading at all?"

| | |
|---|---|
| **Input** | All midday snapshots (~15,000 rows) |
| **Output** | Binary: TRADE / SKIP |
| **Model type** | Best of 11: XGBoost (shallow/balanced/regularized/deep), LightGBM (fast/dart/large), LogisticRegression (balanced/c1/c01) |
| **Feature sets tried** | 7: fo_full, fo_expiry_aware_v2/v3, fo_no_opening_range, fo_no_time_context, fo_oi_pcr_momentum, fo_iv_skew_only |
| **Selection criterion** | Best CV ROC AUC across all 11×7=77 combinations |
| **Gate to pass** | ROC AUC ≥ 0.55, Brier ≤ 0.22 |
| **Typical pass value** | ~0.61–0.63 (XGBoost wins most runs) |
| **If gate fails** | Entire run stops. GATE_FAILED. |

---

### Stage 2 — Direction Gate
> **Question**: "If we trade, should we sell a CE (bearish) or PE (bullish)?"

| | |
|---|---|
| **Input** | Only rows Stage 1 said TRADE (MIDDAY session filter applied) |
| **Output** | CE / PE / NO_TRADE (if confidence too low) |
| **Model type** | LogisticRegression (balanced) |
| **Feature set** | fo_midday_direction_regime_v1 (new in S3) |
| **Key new features** | `oracle_rolling_ce_win_rate_5d/10d`, `oracle_rolling_pe_win_rate_10d`, `ce_pe_win_rate_diff_5d/10d` — rolling oracle win-rates with shift(1) to prevent lookahead |
| **Why these features?** | Stage 2 was learning the dominant regime and projecting it blindly. Rolling oracle stats give it explicit regime memory ("in last 10 days, was CE winning?") |
| **Policy options** | `direction_dual_threshold_v1` (symmetric CE/PE thresholds) or `direction_gate_economic_balance_v1` (balanced CE/PE share enforcement) |
| **Gate to pass** | Part of combined economics gate |

---

### Stage 3 — Recipe Selection
> **Question**: "Which strike + expiry combination gives the best P&L?"

| | |
|---|---|
| **Input** | Rows Stage 2 labelled CE or PE with sufficient confidence |
| **Output** | Recipe ID: L0 (ATM), L3 (3 strikes OTM), L6 (6 strikes OTM), etc. |
| **Model type** | Best of 7: XGBoost, LightGBM, LogisticRegression (OvR multiclass) |
| **Feature sets** | fo_full, fo_expiry_aware_v3, fo_no_time_context |
| **Recipe catalogs** | fixed_l0_l3_v1 (baseline) or midday_l3_adjacent_v1 (expanded) |
| **Policy options** | dynamic (model chooses freely) or fixed_guard (model constrained to baseline recipe unless high confidence to deviate) |

---

## 3. Cross-Validation Mechanics

> **Why rolling CV instead of a simple train/test split?**
> BankNifty regimes shift. A single split gives 1 data point. Rolling CV gives ~40 independent test windows across 4 years — statistically robust.

```
4 YEARS OF DATA  (2020-08-03 → 2024-04-30 ≈ 970 trading days)

 Day:  1────────────────────────────────────────────────────970

 FOLD 1:  [═════TRAIN 84d═════][═VALID 21d═][═TEST 21d═]
 FOLD 2:       [═════TRAIN 84d═════][═VALID 21d═][═TEST 21d═]
 FOLD 3:            [═════TRAIN 84d═════][═VALID 21d═][═TEST 21d═]
   ...                step = 21 days each time
 FOLD 40:                    ...    [═════TRAIN 84d═════][═VALID═][═TEST═]

 Result: ~40 ROC AUC scores → MEAN reported
         Each fold = independent regime sample
         (COVID crash · 2022 bear · 2023 recovery · 2024 election all covered)
```

```
KEY DISTINCTION:
  84 days  = size of each fold's training window  (for model selection)
  4 years  = total span CV slides across          (for robustness)

FINAL MODEL TRAINING (after CV picks the winner):
  Trains on ALL 4 years (2020-08-03 → 2024-07-31)
  Validated on May–Jul 2024 (unseen during CV)
  Final holdout: Aug–Oct 2024 (never touched until publish decision)
```

---

## 4. Campaign Orchestration (What's Running Now)

```
CAMPAIGN: stage3_search_campaign_v1
════════════════════════════════════════════════════════════════════════

  LANE 1: direction_regime_search                       [RUNNING ~3–5 hrs]
  ┌─────────────────────────────────────────────────────────────────┐
  │  Grid: staged_grid.stage3_direction_regime_v1.json              │
  │                                                                  │
  │  Run A: s3_regime_baseline  ──────────────────────────►         │
  │    Stage 1: full 11-model catalog × 7 feature sets × 40 folds  │
  │    Stage 2: fo_midday_direction_regime_v1 + dual_threshold      │
  │    Stage 3: logreg · fixed_l0_l3_v1                             │
  │                                                                  │
  │  Run B: s3_regime_balanced  (reuses Stage 1 from Run A) ──────► │
  │    Stage 2: fo_midday_direction_regime_v1 + economic_balance    │
  │    Stage 3: logreg · fixed_l0_l3_v1                             │
  │                                                                  │
  │  Runs A+B execute in parallel (2 CPUs each from 8 total)        │
  │  Winner selected by: profit_factor → net_return → roc_auc       │
  └─────────────────────────────────────────────────────────────────┘
                              │
                              │  Lane 1 completes → Lane 2 unblocked
                              ▼

  LANE 2: stage3_policy_paths_search                   [PENDING ~4–5 hrs]
  ┌─────────────────────────────────────────────────────────────────┐
  │  Grid: staged_grid.stage3_midday_policy_paths_v1.json           │
  │  Stage 1: REUSED from Lane 1 (no retraining)                    │
  │  Stage 2: fo_midday_direction_regime_v1 applied to all paths    │
  │                                                                  │
  │  6 runs (2 parallel at a time, 3 batches):                      │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ Batch 1: baseline_dynamic  +  balanced_gate_dynamic     │   │
  │  │ Batch 2: balanced_gate_fixed_guard + expanded_dynamic   │   │
  │  │ Batch 3: expanded_fixed_guard  + expanded_relaxed_margin│   │
  │  └─────────────────────────────────────────────────────────┘   │
  │                                                                  │
  │  Variables explored:                                             │
  │    · stage2 policy  (threshold vs balanced gate)                │
  │    · stage3 policy  (dynamic vs fixed_guard)                    │
  │    · recipe catalog (fixed_l0_l3 vs midday_l3_adjacent)         │
  │    · block_expiry   (yes / no)                                  │
  └─────────────────────────────────────────────────────────────────┘
```

---

## 5. Factory State Machine (How Each Lane Is Managed)

```
                      ┌──────────┐
                      │ PENDING  │  ← lane created, waiting to start
                      └────┬─────┘
                           │ budget available + dependencies done
                           ▼
                      ┌──────────────────┐
                      │ WAITING_RESOURCE │  ← cores/memory not free yet
                      └────┬─────────────┘
                           │ budget acquired
                           ▼
                      ┌──────────┐
                      │ RUNNING  │  ← subprocess launched
                      └────┬─────┘
                           │ subprocess exits
              ┌────────────┼────────────────────┐
              ▼            ▼                     ▼
        ┌──────────┐  ┌──────────┐        ┌───────────────┐
        │PUBLISHABLE│  │  HELD   │        │  INFRA_FAILED │
        │(deploy!) │  │(logged) │        │  (retry ≤ 2x) │
        └──────────┘  └──────────┘        └───────────────┘
              │              │                    │
              │              │            ┌───────┴──────┐
              │              │            │ attempt < max│
              │              │            └──────┬───────┘
              │              │                   │
              │              │            ┌──────▼──────┐
              │              │            │   PENDING   │  (retry)
              │              │            └─────────────┘
              │              │
              │         ┌────▼──────────┐
              │         │ GATE_FAILED   │  ← economics/ROC below threshold
              │         └───────────────┘
              │
   downstream lanes cancelled if dependency failed
```

---

## 6. Decision Gates — What Makes a Run "Publishable"

```
                         Run completes
                              │
              ┌───────────────▼───────────────┐
              │      STAGE 1 CV GATE           │
              │  ROC AUC ≥ 0.55                │
              │  Brier   ≤ 0.22                │
              └───────────────┬───────────────┘
                         FAIL │ PASS
                              │
              ┌───────────────▼───────────────┐
              │      COMBINED HOLDOUT GATE     │
              │  profit_factor   ≥ 1.5         │
              │  net_return      > 0           │
              │  trades          ≥ 50          │
              │  max_drawdown    ≤ 10%         │
              │  side_share      ∈ [30%, 70%]  │
              └───────────────┬───────────────┘
                         FAIL │ PASS
                              │
                    ┌─────────┴──────────┐
                    ▼                    ▼
                  HELD              PUBLISHABLE
              (metrics kept,        (winner candidate,
               not deployed)         ranked by strategy)

RANKING STRATEGY: publishable_economics_v1
  1st: profit_factor DESC
  2nd: net_return_sum DESC
  3rd: stage2_roc_auc DESC
  4th: lane_id ASC  (tiebreak — deterministic)
```

---

## 7. S3 Direction Fix — Root Cause & Solution

```
PROBLEM (S0–S2, all failed):
  Stage 2 had 0/24 features with cross-window stable CE/PE separation.
  Root cause: model learned the dominant regime of the training window
  and projected it forward blindly.

  Example of what was happening:
    Training window: 60% CE days → model learns "usually CE"
    Validation:      50/50 oracle → model still says "CE" → +14pp CE side
    Holdout:         50/50 oracle → model still says "CE" → +31pp CE side
    (Balanced 50/50 oracle both times — model was WRONG, not just biased)

SOLUTION (S3):
  Give Stage 2 explicit regime memory via rolling oracle statistics:

  oracle_rolling_ce_win_rate_5d   = (CE winning days in last 5d) / total
  oracle_rolling_pe_win_rate_5d   = (PE winning days in last 5d) / total
  ce_pe_win_rate_diff_5d          = CE rate − PE rate
  (same for 10d window)

  Key design decisions:
  ✓ shift(1) applied → today's features use ONLY prior days (no lookahead)
  ✓ Computed from oracle targets, not live prices (training-time only)
  ✓ Dropped 12 confirmed regime-follower features (PCR, IV skew, dist_from_day)
  ✓ Added regime binary flags (ctx_regime_atr_high, regime_trend_up, etc.)

  Live inference gap (deferred to S4):
  ✗ Rolling oracle stats require daily oracle lookback table at inference time
    → not yet wired into live ml_pure lane
    → must be resolved before publishing to production
```

---

## 8. Infrastructure

```
TRAINING VM: option-trading-ml-01
  Machine:    n2-highmem-8  (8 vCPUs, 64 GB RAM)
  Zone:       asia-south1-b
  Project:    amittrading-493606
  Quota ceiling: 12 CPU total across regions (not adjustable)

DATA LOCATION:
  /home/savitasajwan03/option_trading/
  .data/ml_pipeline/parquet_data/snapshots_ml_flat/year=YYYY/data.parquet
  Years: 2020–2024

ARTIFACT LOCATION:
  ml_pipeline_2/artifacts/campaign_runs/{campaign_id}/
    workflow_state.json          ← live status of all lanes
    campaign_result.json         ← final outcome
    lanes/{01_lane_id}/
      factory_lane.log           ← subprocess stdout/stderr
      runner_output/
        grid_status.json         ← grid runner status
        runs/{01_run_id}/
          summary.json           ← metrics, winner, blocking reasons
          stages/stage1/model.joblib  ← trained model artifacts

CAMPAIGN CLI:
  python -m ml_pipeline_2.run_training_campaign \
    --spec ml_pipeline_2/configs/campaign/stage3_search_campaign_v1.json

MONITOR LIVE STATUS:
  grep -E "status|last_error|completed_at" \
    ml_pipeline_2/artifacts/campaign_runs/stage3_search_campaign_v1/workflow_state.json
```

---

## 9. Experiment History

| Run | Stage | Result | Key finding |
|-----|-------|--------|-------------|
| stage2_midday_v1 | S2 baseline | HELD | Direction model learns regime, not edge |
| stage2_scenarios_v1/v2 | S2 feature search | GATE_FAILED | PCR/IV features are regime followers |
| stage2_midday_direction_or_no_trade_v1 | S2 redesign | GATE_FAILED | Target redesign helped Stage 1, not Stage 2 |
| stage2_midday_target_redesign_v1 | S2 target fix | HELD | Stage 1 passes (0.62), Stage 2 still poor |
| stage3_midday_policy_paths_v1 | S3 policy search | HELD | Best Stage 1 (0.619), Stage 2 ROC ~0.54 |
| stage3_direction_regime_v1 | S3 feature signal | GATE_FAILED | 0/24 features stable — confirmed regime amplifier |
| Grid A/B label fix | direction_market_up_v1 | HELD | Label bias fixed (93.8%→51% CE). S2 ROC 0.544–0.545. VOLATILE PF=1.31–1.82 |
| staged_deep_hpo_c1_base_20260429 | Grid C deep HPO | HELD + **FORCE-DEPLOYED** | S1=0.683, S2=0.591. VOLATILE PF=1.314, TRENDING PF=0.306. Deployed with regime_gate_v1 (VOLATILE+SIDEWAYS only) |
| staged_deep_hpo_d2_high_edge_20260501 | Grid D high-edge HPO | HELD | S1=0.855, S2=0.618, combined PF=1.194, MDD=29.5%, block_rate=3.97%. PRE_EXPIRY+UNKNOWN drag |
| staged_deep_hpo_e1_volatile_only_20260501 | Grid E VOLATILE-only S2 | GATE_FAILED | stage2_signal_check 0 samples — session_filter config bug (bucket vs regime column mismatch) |

---

## 10. Publish Criteria (What "Done" Looks Like)

A run is publishable when **all** of the following hold on the **final holdout** (Aug–Oct 2024, never seen during training):

```
profit_factor   ≥ 1.50   (gross profit / gross loss)
net_return      > 0.00   (positive P&L after costs)
trades          ≥ 50     (statistically meaningful sample)
max_drawdown    ≤ 10%    (risk control)
side_share CE   ≥ 30%    (not degenerate CE-only)
side_share PE   ≥ 30%    (not degenerate PE-only)
```

If publishable: model artifacts are copied to the live model group path and the `ml_pure` lane picks them up at next restart.

If held but not publishable: metrics are logged, run archived, escalate to S4/S5 review.

# Stage 3 Recipe Model Trade Loss Analysis

## Date: 2026-04-23
## Run: `expiry_bypass_stage2_test_v1_20260423_013438`

---

## 1. Executive Summary

The `bypass_stage2` pipeline now produces **1,432 trades** (fixing the 0-trade bug). However, all trades **lose money** (net_return = -0.83, profit_factor = 0.35). This analysis identifies **four root causes**.

---

## 2. Stage-by-Stage Signal Quality

| Stage | roc_auc | Signal Quality | Impact |
|-------|---------|---------------|--------|
| **Stage 1** (entry) | **0.686** | ✅ Good | Correctly identifies entry candidates (~15% of rows) |
| **Stage 2** (direction) | **0.500** | ⚠️ Random (bypassed) | 50/50 coin flip for CE/PE |
| **Stage 3** (recipe) | **N/A** | ❌ No signal | Models have zero predictive power |

**Combined result:** Entry filter works → Direction is random → Recipe selection is random → **Transaction costs guarantee losses.**

---

## 3. Root Cause Analysis

### Cause 1: Recipe Models Have No Predictive Power

The Stage 3 OVR (One-vs-Rest) recipe models are trained to predict which recipe will succeed. When each recipe is used as a **fixed baseline** (no dynamic selection), ALL lose money:

| Recipe | Trades | Net Return | Profit Factor | Win Rate |
|--------|--------|-----------|---------------|----------|
| L0 | 44,366 | -26.21 | 0.26 | 22% |
| L1 | 44,366 | -26.34 | 0.25 | 23% |
| L2 | 44,366 | -26.30 | 0.26 | 25% |
| L3 | 44,366 | -25.77 | 0.31 | 25% |
| L4 | 44,366 | -25.83 | 0.29 | 23% |
| L5 | 44,366 | -25.87 | 0.30 | 26% |
| **L6** (best) | 44,366 | **-25.39** | **0.33** | **26%** |

**Finding:** No recipe is profitable. The "best" recipe (L6) still loses 25.39 points. This is a **fundamental problem with the recipe definitions or labels** — not a threshold/gate issue.

### Cause 2: Recipe Selection Is Unprofitable at All Thresholds

Stage 3 policy tried 12 threshold/margin combinations on validation. ALL produced losing trades:

| Threshold | Margin | Trades | Net Return | Profit Factor |
|-----------|--------|--------|-----------|---------------|
| 0.45 | 0.02 | 12,050 | -6.73 | 0.41 |
| 0.50 | 0.02 | 8,242 | -4.52 | 0.43 |
| 0.55 | 0.02 | 5,698 | -3.11 | 0.44 |
| 0.60 | 0.02 | 4,012 | -2.14 | 0.45 |

**Finding:** Higher thresholds reduce losses by selecting fewer trades, but ALL are unprofitable. The recipe models have **no signal** — they cannot distinguish winning from losing trades.

### Cause 3: Direction is Random (Stage 2 Bypassed)

With `bypass_stage2`, `direction_up_prob = 0.5` for all rows:
- **Long share: 50.0%**
- **Short share: 50.0%**

The direction gate fix correctly allows both CE and PE through, but direction is a coin flip. Even with a perfect recipe selector, random direction × transaction costs = losses.

### Cause 4: Transaction Costs Overwhelm Weak Signal

When direction is random and recipe selection adds no edge, every trade has ~50% win probability. After transaction costs (spread + slippage + fees), expected value is negative. The profit factor of 0.35 means for every $1 lost, only $0.35 is won — consistent with random trading after costs.

---

## 4. Scenario Breakdown (Where Trades Come From)

### By Regime

| Regime | Rows | Trades | Block Rate | Net Return | PF |
|--------|------|--------|-----------|------------|-----|
| TRENDING | 9,605 | 554 | 94.2% | -0.33 | 0.34 |
| VOLATILE | 4,765 | 526 | 89.0% | -0.30 | 0.39 |
| PRE_EXPIRY | 4,886 | 238 | 95.1% | -0.12 | 0.41 |
| UNKNOWN | 2,405 | 84 | 96.5% | -0.05 | 0.06 |
| SIDEWAYS | 2,398 | 30 | 98.7% | -0.02 | 0.13 |

### By Session

| Session | Rows | Trades | Block Rate | Net Return | PF |
|---------|------|--------|-----------|------------|-----|
| FIRST_HOUR | 3,840 | 546 | 85.8% | -0.30 | 0.45 |
| LAST_HOUR | 3,899 | 670 | 82.8% | -0.40 | 0.22 |
| MID_SESSION | 16,320 | 216 | 98.7% | -0.13 | 0.41 |

**Finding:** Most trades occur in First Hour and Last Hour (higher volatility). Even these "best" time windows lose money because direction/recipe have no edge.

---

## 5. Key Metrics

| Metric | Value | Interpretation |
|--------|-------|---------------|
| **Total trades** | 1,432 | Pipeline generates trades now |
| **Block rate** | 94.0% | Heavy filtering but still wrong selections |
| **Win rate** | 22.6% | Below random (50%) — confirms no edge |
| **Profit factor** | 0.35 | Losing $0.65 per $1 risked |
| **Max drawdown** | 56.8% | Severe capital erosion |
| **CE/PE balance** | 50%/50% | Bypass working correctly |
| **Selected recipe** | L0 | Arbitrary (all recipes lose similarly) |

---

## 6. Recipe Model Training Issues

### Evidence of Training Problems

1. **Final fit report shows only 1 experiment** per recipe — the final fit disables HPO by design (model_specs_override is set)
2. **Search report is NOT saved** — only final fit report is persisted to `training_report.json`
3. **Model metadata shows `search_origin: "override"`** — this is the fallback/final-fit model, not a searched model
4. **No roc_auc/brier/accuracy in training report** — model evaluation metrics are missing

### Why This Matters

The `train_recipe_ovr_stage` function calls `_training_call` twice:
1. **Search call** — should run HPO with 4 models × 2 feature sets × 3 trials = ~24 experiments
2. **Final fit call** — fits the best model with HPO disabled → 1 experiment

Only the final fit report is saved. We cannot verify if the search found good models or if all 24 experiments failed.

---

## 7. Hypotheses

| # | Hypothesis | Evidence | Test |
|---|-----------|----------|------|
| H1 | Recipe definitions are fundamentally unprofitable | All 7 recipes lose as fixed baselines | Compare with non-bypass baseline run |
| H2 | Recipe labels (move_barrier_hit) have no feature relationship | All models have PF < 1.0 | Check feature importance / permutation importance |
| H3 | Stage 2 direction model is the real profit driver | Bypass removes the only edge | Run non-bypass baseline and compare |
| H4 | Recipe OVR training has a bug (not actually searching) | Only 1 experiment in saved report | Save and inspect search payload |
| H5 | Too many recipes dilute signal (7 recipes × ~14% base rate) | OVR models struggle with rare positive class | Test with 4 recipes (fixed_l0_l3_v1) |

---

## 8. Investigation Runs Launched

Three test scenarios launched on VM at 05:07 UTC to isolate the issue:

| Scenario | Catalog | Threshold Grid | Margin Grid | Session |
|----------|---------|---------------|-------------|---------|
| **Fixed Catalog** | `fixed_l0_l3_v1` (4 recipes) | [0.45, 0.5, 0.55, 0.6] | [0.02, 0.05, 0.1] | stage3_inv_fixed |
| **Low Thresholds** | `midday_l3_adjacent_v1` (7 recipes) | [0.2, 0.25, 0.3, 0.35, 0.4, 0.45] | [0.0, 0.01, 0.02, 0.05] | stage3_inv_lowth |
| **Combined** | `fixed_l0_l3_v1` (4 recipes) | [0.2, 0.25, 0.3, 0.35, 0.4, 0.45] | [0.0, 0.01, 0.02, 0.05] | stage3_inv_comb |

### Expected Outcomes

| Hypothesis | If True |
|-----------|---------|
| H1 (threshold too high) | Low-threshold runs produce profitable trades |
| H2 (too many recipes) | Fixed-catalog runs produce profitable trades |
| H3 (models have no signal) | ALL runs still lose — need to fix recipe model training |

---

## 9. Recommendations

### Immediate (Today)
1. Wait for investigation runs to complete (~2-4 hours)
2. Compare results to determine which hypothesis is correct

### If H1 or H2 is True (Threshold/Catalog Issue)
1. Update default manifest with winning configuration
2. Run full non-bypass baseline to confirm recipe model works with real Stage 2

### If H3 is True (Models Have No Signal)
1. **Debug recipe model training:** Save search payload reports, not just final fit
2. **Inspect recipe labels:** Verify `move_barrier_hit` labels are meaningful
3. **Run permutation test:** Shuffle labels and confirm performance is identical
4. **Check feature importance:** See if any features have signal for recipe prediction

### If All Hypotheses Fail
1. **Run non-bypass baseline** — confirm Stage 2 direction model is the real profit driver
2. If non-bypass baseline is profitable, `bypass_stage2` is confirmed as a debug-only mode
3. Focus on improving Stage 2 model quality instead

---

## 10. Code Changes Made

1. `pipeline.py:3406-3438` — `bypass_stage2` skips `stage2_signal_check` pre-gate
2. `pipeline.py:2221-2243` — `_stage2_side_masks_from_policy` detects bypass dummy data and bypasses direction gate
3. `manifests.py:313-316` — `_validate_stage1_reuse` returns Path objects

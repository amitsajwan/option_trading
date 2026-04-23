# Stage 3 Recipe Model Failure Investigation

## Date: 2026-04-23

## 1. Problem Statement

Stage 3 (recipe selection) of the staged dual-recipe pipeline consistently produces **0 trades**, even when Stage 1 and Stage 2 complete successfully. This blocks the entire pipeline from generating actionable trading signals.

## 2. Investigation Methodology

We used `bypass_stage2` mode to isolate Stage 3 and test it independently of Stage 2 direction model quality.

## 3. Key Findings

### 3.1 Bypass Run Results

| Run | Recipe Catalog | Threshold Grid | Margin Grid | Result |
|-----|---------------|----------------|-------------|--------|
| bypass_stage2_test_v1 (2026-04-22) | midday_l3_adjacent_v1 (7 recipes) | [0.45, 0.5, 0.55, 0.6] | [0.02, 0.05, 0.1] | **0 trades** |

**Blocking reasons:**
- `stage2_signal_check.max_corr=0.0483<0.05` (pre-gate failure — Stage 2 signal too weak)
- Stage 3 policy selected: `threshold=0.45, margin_min=0.02` (lowest values in grid)
- All 12 threshold/margin combinations produced 0 trades on validation

### 3.2 Root Cause Analysis

#### Cause A: Stage 2 Direction Gate Blocks All Trades

When `bypass_stage2` injects dummy probabilities (`direction_up_prob=0.5`, `direction_trade_prob=1.0`), the direction gate policy (`direction_gate_economic_balance_v1`) fails:

- `ce_threshold = 0.55`: `0.5 >= 0.55` → **FALSE**
- `pe_threshold = 0.55`: `0.5 >= 0.55` → **FALSE**
- `min_edge = 0.05`: `|0.5 - 0.5| = 0.0 >= 0.05` → **FALSE**

Result: **CE mask = False, PE mask = False** → 0 trades reach Stage 3.

**Fix implemented (2026-04-23):** `_stage2_side_masks_from_policy` now detects bypass dummy data (`direction_up_prob ≈ 0.5` and `direction_trade_prob ≈ 1.0`) and returns both masks as `entry_mask`, allowing all entries to pass.

#### Cause B: Stage 3 OVR Recipe Model May Produce Low Probabilities

Stage 3 uses **One-vs-Rest (OVR)** training with `ovr_recipe_catalog_v1`:
- Each recipe gets an independent binary classifier
- Probabilities are NOT softmax-normalized
- With 7 recipes (`midday_l3_adjacent_v1`), per-recipe base rate ≈ 14%
- Binary classifiers may output probabilities < 0.45 even for their "best" cases

The recipe selection gate requires:
- `recipe_prob >= threshold` (minimum 0.45 in grid)
- `recipe_prob - second_best_prob >= margin_min` (minimum 0.02)

If max recipe probability is ~0.3, no recipe passes the gate → 0 trades.

## 4. Investigation Scenarios Prepared

Three test manifests created to isolate the issue:

### Scenario 1: Reduced Catalog (4 recipes)
**File:** `staged_single_run.bypass_stage2_fixed_catalog.json`
- Uses `fixed_l0_l3_v1` (4 recipes instead of 7)
- Per-recipe base rate increases from ~14% to ~25%
- Easier for binary classifiers to exceed 0.45 threshold

### Scenario 2: Lowered Thresholds (6 values)
**File:** `staged_single_run.bypass_stage2_low_threshold.json`
- Recipe threshold grid: `[0.2, 0.25, 0.3, 0.35, 0.4, 0.45]`
- Margin grid: `[0.0, 0.01, 0.02, 0.05]`
- Tests whether probabilities are below 0.45 or just below 0.45

### Scenario 3: Combined (4 recipes + low thresholds)
**File:** `staged_single_run.bypass_stage2_combined.json`
- Most permissive configuration
- Tests if Stage 3 can produce ANY trades when both catalog and thresholds are relaxed

## 5. Hypotheses

| Hypothesis | Test | Expected Outcome |
|-----------|------|------------------|
| H1: Threshold too high | Scenario 2 | Trades appear at threshold < 0.45 |
| H2: Too many recipes dilute signal | Scenario 1 | Trades with 4 recipes but not 7 |
| H3: Stage 3 model fundamentally weak | Scenario 3 | If still 0 trades → model issue |
| H4: Stage 2 needed for good Stage 3 | Non-bypass baseline | Compare with normal run |

## 6. Next Steps

1. **Launch investigation runs** on VM (bypass_stage2_fixed_catalog, bypass_stage2_low_threshold, bypass_stage2_combined)
2. **Compare results** — which scenario produces trades?
3. **If all fail:** Investigate Stage 3 feature set (`fo_velocity_v1`, `fo_full`) and model capacity
4. **If one succeeds:** Use that configuration as new default and test with non-bypass Stage 2

## 7. Files

- `configs/research/staged_single_run.bypass_stage2_fixed_catalog.json`
- `configs/research/staged_single_run.bypass_stage2_low_threshold.json`
- `configs/research/staged_single_run.bypass_stage2_combined.json`

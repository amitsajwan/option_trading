# Recovery Baseline — April 22, 2026

## Context

Remote VM: `option-trading-ml-01` (asia-south1-b)
User context: `savitasajwan03`

## Verified Remote Run Roots

Actual paths on VM:
- `vel_dir_grid_20260419_092647` → logs at `~/option_trading/logs/vel_dir_grid_20260419_092622/` (artifact root similar)
- `expiry_dir_grid_20260419_105402` → logs at `~/option_trading/logs/expiry_dir_grid_20260419_105339/`
- `soft_gate_grid_20260418_094708` — not found on VM (likely cleaned up)

## Best Anchor Run

**Run:** `expiry_dir_grid_20260419_105402 / 01_expiry_s2_midday`
**Artifact dir:**
`/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/training_launches/expiry_dir_grid_20260419_105402/run/runs/01_expiry_s2_midday`

### Results Summary

| Stage | Metric | Value | Gate |
|-------|--------|-------|------|
| Stage 1 (entry) | roc_auc | 0.6183 | PASS |
| | brier | 0.1916 | PASS |
| | roc_auc_drift_half_split | 0.0301 | PASS |
| Stage 2 (direction) | roc_auc | 0.5710 | record_only (soft) |
| | brier | 0.2524 | PASS (relaxed 0.26) |
| Stage 3 (recipe) | — | — | Passed CV gate |
| Combined Holdout | trades | 185 | FAIL |
| | profit_factor | 0.3786 | FAIL (<1.5) |
| | net_return_sum | -0.0940 | FAIL (<0.0) |
| | max_drawdown_pct | 0.0982 | PASS (<0.10) |
| | long_share | 0.9135 | FAIL (>0.70) |
| | short_share | 0.0865 | FAIL (<0.30) |
| | selected_recipes | ["L0"] | — |

### Why It Failed

1. **Stage 3 recipe selection is unprofitable on ALL validation grid points.**
   - Every (threshold, margin) combination produces negative `net_return_sum` and `profit_factor < 1.0`.
   - The "best" grid point (threshold=0.60, margin=0.02) is simply the least negative (-0.337 net_return on 830 trades).
   - Holdout then amplifies this to -0.094 on only 185 trades.

2. **Stage 2 direction collapses to CE bias on holdout.**
   - Validation side share was roughly balanced (35% CE / 65% PE at selected policy).
   - Holdout side share is 91.3% CE / 8.7% PE.
   - This suggests either:
     a) regime shift in Aug-Oct 2024 (CE-dominant market), or
     b) Stage 2 model overfits to validation-period side distribution.

3. **The stacked pipeline compounds errors.**
   - Stage 1 → Stage 2 → Stage 3 is a narrow funnel.
   - Each stage filters data, leaving too few samples for downstream stages to generalize.

### Policy Details (Selected)

- **Stage1:** threshold=0.45 (very permissive, letting through 22,441 of 23,412 rows)
- **Stage2:** trade_threshold=0.45, ce_threshold=0.55, pe_threshold=0.60, min_edge=0.05
- **Stage3:** recipe_threshold=0.60, recipe_margin_min=0.02, selection_mode=dynamic

## Config Fragility Note

The expiry grid config hardcodes:
```json
"stage1_reuse": {
  "source_run_id": "vel_s2_midday",
  "source_run_dir": "/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/training_launches/vel_dir_grid_20260419_092647/run/runs/01_vel_s2_midday"
}
```
This is an absolute path tied to a specific run. If that run is cleaned up, the config breaks.

## Implemented Changes

### 1. bypass_stage2 Pipeline Support

Added a `bypass_stage2` flag in `manifest.training.bypass_stage2`. When enabled:
- **Skips Stage 2 direction model training entirely**
- Injects neutral dummy probabilities (`direction_up_prob=0.5`, `direction_trade_prob=1.0`)
- **Evaluates both CE and PE trades independently** for every Stage 1 entry signal (dual-side execution)
- Stage 3 recipe selection still runs on top of the combined trades
- Auto-passes Stage 2 CV gate

Modified files:
- `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`
  - `_score_stage2_package`: recognizes `_bypass_stage2` sentinel package
  - `_add_upstream_probs`: injects dummy Stage 2 columns when bypassed
  - `_evaluate_combined_policy` / `_combined_policy_trade_rows`: new `dual_side_mode` parameter that generates two trades per entry (CE + PE) instead of picking one side
  - `select_recipe_policy` / `select_recipe_economic_balance_policy` / `select_recipe_fixed_baseline_guard_policy` / `_fixed_recipe_baseline`: threaded `dual_side_mode` through all policy evaluation paths
  - `_create_bypass_stage2_result`: helper that builds a minimal dummy Stage 2 result with bypass-sentinel packages and dummy score DataFrames
  - `run_staged_research`: reads `bypass_stage2` from manifest, creates dummy result when set, skips diagnostics/gates, and passes `dual_side_mode` to combined policy evaluation

- `ml_pipeline_2/src/ml_pipeline_2/contracts/manifests.py`
  - Added `bypass_stage2` to the allowed `training` keys list
  - Added `_search_for_run_dir` helper that searches `artifacts/training_launches/*/*/run/runs/{source_run_id}` when an absolute `source_run_dir` doesn't exist, making `stage1_reuse` configs portable across machines

- `ml_pipeline_2/configs/research/staged_dual_recipe.expiry_direction_v1.json`
  - Added `"bypass_stage2": false` to make the option discoverable

### 2. Config Pathing Fix

`_validate_stage1_reuse` now falls back to a filesystem search under `artifacts/training_launches/` and `artifacts/research/` when the explicit `source_run_dir` is missing. This means grid configs with hardcoded absolute paths (e.g., `/home/savitasajwan03/...`) will still resolve if the run exists under the current workspace's artifact roots.

## How to Test bypass_stage2

Add `"bypass_stage2": true` to the `training` section of any staged manifest and launch a grid or single run. The summary will show:
- `stage_artifacts.stage2` with `bypass_stage2: true` in diagnostics
- Combined holdout summary with roughly 50/50 CE/PE side share (instead of degenerating to one side)
- Trades count roughly doubles compared to single-side execution

## Next Steps

1. **Run a new grid with `bypass_stage2: true`** on the expiry or velocity manifests to see if dual-side execution passes the combined holdout gate.
2. **If it passes**, promote `bypass_stage2` as the default for the next training campaign.
3. **If it still fails**, the issue is either:
   - Stage 1 entry signal is not selective enough (threshold too low)
   - Stage 3 recipe model has no predictive power (all recipes lose money)
   - Market regime in Aug-Oct 2024 is genuinely unfavorable
4. Continue investigating with counterfactual / confidence-execution diagnostics on the VM once they complete.

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

## Next Steps

1. **Run diagnostics** on `01_expiry_s2_midday`:
   - `run_stage12_counterfactual`
   - `run_stage12_confidence_execution`
   - `run_stage12_confidence_execution_policy`
   - `run_stage12_dual_side_policy`

2. **Evaluate `bypass_stage2` branch.** The 3-stage stack is not adding value. Consider:
   - Stage 1 entry → fixed recipe (e.g., L3) with dual-side execution
   - Or Stage 1 entry → direct recipe selection without direction gating

3. **Fix config pathing.** Replace absolute `source_run_dir` with relative or registry-based lookup.

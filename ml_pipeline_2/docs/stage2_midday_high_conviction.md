# Stage 2 MIDDAY High-Conviction

## Purpose

This batch narrows the Stage 2 problem on purpose.

The prior `MIDDAY` target-redesign batch proved three things:

- `MIDDAY` remains the only viable Stage 2 regime
- target pruning helps, but the binary direction task still misses publish-grade Brier
- the next lever is selectivity, not more broad feature search

The high-conviction batch therefore keeps the existing binary `CE` vs `PE` target, but only after:

- hard target pruning on edge, winner return, and opposing-side loss
- optional post-pruning down-selection to the top-conviction fraction of rows

## Target Rule

`training.stage2_target_redesign` now supports:

- `enabled`
- `min_directional_edge_after_cost`
- `min_winner_return_after_cost`
- `max_opposing_return_after_cost`
- `max_kept_fraction`
- `conviction_score`

The redesign acts in two passes:

1. hard thresholds remove ambiguous rows
2. if `max_kept_fraction < 1.0`, keep only the top-conviction share

Supported conviction scores:

- `edge`
- `winner_return`
- `edge_winner_min`

`edge_winner_min` is the default research choice because it favors rows that are strong on both winner quality and directional separation.

## Batch Design

This grid is intentionally small:

- `midday_asymmetry_high_conviction_baseline`
- `midday_asymmetry_high_conviction_strict`
- `midday_oi_iv_high_conviction`
- `midday_expiry_high_conviction`
- `midday_asymmetry_ultra_selective`

Rules:

- `MIDDAY` only
- Stage 1 reused from the first lane
- Stage 2 search budgets stay on
- no broad regime variants

## Success Criteria

Promote only if the winner improves all three:

- Stage 2 validation Brier materially below the current `0.2474` baseline
- Stage 2 ROC-AUC still at or above `0.55`
- robustness gate-pass rate is no longer effectively zero

If the high-conviction batch still leaves Brier far above `0.22`, the next step is a true `direction-or-no-trade` Stage 2 formulation.

## Recommended Command

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_midday_high_conviction_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_midday_high_conviction_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

# Stage 2 MIDDAY Redesign

## Purpose

This is the first focused redesign batch after the `MIDDAY`-only grid.

The previous `MIDDAY` grid established:

- `midday_time_aware_pool` is the best current Stage 2 branch
- validation Stage 2 ROC-AUC is already above gate
- validation Stage 2 Brier remains the only material blocker
- further regime search is no longer justified

The next cycle therefore keeps the binary CE-vs-PE target and improves only:

- Stage 2 label cleanliness inside `MIDDAY`
- Stage 2 directional-separation features inside `MIDDAY`

## Baseline

Use `midday_time_aware_pool` as the redesign baseline.

Current quality:

- validation Stage 2 ROC-AUC: about `0.577`
- validation Stage 2 Brier: about `0.252`
- dominant blocker: `stage2_cv.brier>0.22`

## Redesign Lanes

- `midday_time_aware_baseline`
- `midday_strict_winner_v2`
- `midday_stricter_abstain`
- `midday_iv_oi_plus_time`
- `midday_expiry_interactions`
- `midday_asymmetry_pool`

## Feature Intent

- `fo_midday_asymmetry`
  - CE/PE relative imbalance and asymmetry signals
- `fo_midday_expiry_interactions`
  - expiry-state and IV/OI relationships that matter during MIDDAY
- `fo_midday_time_aware_plus_oi_iv`
  - the current time-aware baseline plus IV/OI directional-separation signals

These are designed as reusable catalog feature sets, not one-off manifest patches.

## Label Variants

- baseline:
  - `min_directional_edge_after_cost = 0.0014`
  - `max_opposing_return_after_cost = 0.0`
- stricter winner:
  - `max_opposing_return_after_cost = -0.0002`
- stricter abstain:
  - `min_directional_edge_after_cost = 0.0018`
  - `max_opposing_return_after_cost = -0.0004`

## Ranking Rule

The redesign grid should prefer:

1. lower validation Brier
2. higher validation ROC-AUC
3. stronger holdout consistency
4. lower drift
5. better robustness gate-pass behavior

If the redesign winner still sits near `0.25` Brier, the next step is a true Stage 2 target redesign, not another feature/filter grid.

## Recommended Command

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_midday_redesign_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_midday_redesign_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

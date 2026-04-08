# Stage 2 MIDDAY Target Redesign

## Purpose

This batch moves the redesign into the Stage 2 target itself.

The prior MIDDAY grids established:

- the viable regime is `MIDDAY`
- the best feature branch is asymmetry/time-aware within `MIDDAY`
- feature and label-filter tuning improved Brier only modestly
- the remaining blocker is structural target noise

The next cycle therefore changes Stage 2 training rows before signal checks and model search by dropping ambiguous directional rows at label-construction time.

## Target Rule

The redesign remains binary `CE` vs `PE`, but only for rows that satisfy all of:

- minimum directional edge after cost
- minimum winner return after cost
- opposing side capped below a configured threshold

This is a true target redesign because ambiguous rows are removed before Stage 2 signal checks, diagnostics, and training.

## Manifest Knob

Use `training.stage2_target_redesign`:

- `enabled`
- `min_directional_edge_after_cost`
- `min_winner_return_after_cost`
- `max_opposing_return_after_cost`
- `max_kept_fraction`
- `conviction_score`

This is independent from the older Stage 2 label filter. The redesign acts first; the legacy label filter can still be used afterward for additional pruning if needed.

Supported conviction scores:

- `edge`
- `winner_return`
- `edge_winner_min`

Use `max_kept_fraction < 1.0` only when the goal is to keep the highest-conviction tail of already-valid rows. `edge_winner_min` is the most conservative choice because it favors rows that are strong on both separation and winner quality.

## Baseline

Use `midday_asymmetry_pool` as the last pre-redesign evidence anchor.

For the narrower follow-up batch, see `stage2_midday_high_conviction.md`.

## Recommended Command

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_midday_target_redesign_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_midday_target_redesign_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

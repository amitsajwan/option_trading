# Stage3 MIDDAY Policy Paths

> Historical research note. Not the current operating instruction. Use `intraday_profit_execution_plan.md` and `midday_recovery_handover.md` for current status and next steps.

This batch freezes the hierarchical MIDDAY Stage 2 setup and moves the search to Stage 3 policy and recipe selection.

Base manifest:
- `ml_pipeline_2/configs/research/staged_dual_recipe.stage3_policy_paths_v1.json`

Grid manifest:
- `ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json`

Paths covered:
- baseline dynamic Stage 3 with the current recipe catalog
- side-balance-aware Stage 2 policy reuse
- fixed-recipe fallback guard against weak dynamic selectors
- modest L3-adjacent recipe expansion
- relaxed Stage 3 threshold and margin path

Lanes:
- `stage3_baseline_dynamic`: current recipe catalog, current Stage 2 policy, dynamic Stage 3 economic-balance selector
- `stage3_balanced_gate_dynamic`: current recipe catalog, reused Stage 2 model, balanced Stage 2 gate selector, dynamic Stage 3 selector
- `stage3_balanced_gate_fixed_guard`: current recipe catalog, reused Stage 2 model, balanced Stage 2 gate selector, fixed-recipe fallback guard
- `stage3_expanded_catalog_dynamic`: expanded L3-adjacent recipe catalog, Stage 1 reuse only, dynamic Stage 3 selector
- `stage3_expanded_catalog_fixed_guard`: expanded L3-adjacent recipe catalog, Stage 1 reuse only, fixed-recipe fallback guard
- `stage3_expanded_catalog_relaxed_margin`: expanded L3-adjacent recipe catalog, Stage 1 reuse only, wider Stage 3 threshold/margin sweep

Notes:
- grid ranking is explicitly scoped with `selection.ranking_strategy = stage3_policy_paths_v1`
- expanded recipe-catalog lanes reuse Stage 1 only; they do not reuse Stage 2 because the Stage 2 target depends on the active recipe catalog
- fixed-recipe Stage 3 is now a first-class runtime policy mode; `selection_mode = fixed_recipe` requires a valid `selected_recipe_id`

What to look for in results:
- `stage2_cv` should remain strong on reused or retrained lanes; this batch should not regress the solved Stage 2 problem
- `stage3.non_inferior_to_fixed_recipe_baseline_failed` should disappear on at least one lane
- combined holdout should clear:
  - `net_return_sum > 0`
  - `trades >= 50`
  - `side_share_in_band == True`
- if all lanes still fail on economics, stop policy-only iteration and move to Stage 3 label/view redesign

Run:

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

# Stage1+Stage2 Counterfactual Analysis

This is a post-run analysis tool for answering a specific question:

- if Stage 1 and Stage 2 were already good enough,
- what happens if we keep only the top-confidence Stage1+Stage2 holdout trades,
- and then compare fixed recipe execution against an oracle upper bound for the chosen side?

It does not retrain anything. It reuses one completed staged run directory.

## What it measures

For one completed staged run, it:

- reloads the saved `resolved_config.json`
- rescoring Stage 1 and Stage 2 holdout rows from the saved model packages
- applies the selected Stage 1 and Stage 2 policies from `summary.json`
- ranks the surviving trades by:
  - `entry_prob * direction_trade_prob * selected_side_prob`
- evaluates top-confidence subsets such as:
  - `100%`
  - `50%`
  - `33%`
  - `25%`
  - `10%`

For each subset it reports:

- oracle selected-side upper bound
  - best available recipe return for the already-chosen Stage 2 side
- fixed-recipe execution
  - for example `L3`
  - for example `L6`

## Why this exists

This is the fastest way to distinguish between two failure modes:

1. Stage 3 is the real bottleneck
- Stage1+Stage2 subsets look decent under fixed or oracle execution

2. The edge is already too weak before Stage 3
- even the best Stage1+Stage2 subsets stay negative

## Run

```bash
cd ~/option_trading
. .venv/bin/activate

python -m ml_pipeline_2.run_stage12_counterfactual \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --top-fractions 1.0 0.5 0.33 0.25 0.1 \
  --fixed-recipes L3 L6
```

If you want an explicit output location:

```bash
python -m ml_pipeline_2.run_stage12_counterfactual \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --output-root ml_pipeline_2/artifacts/analysis/stage12_counterfactual_midday_gate_oi_iv \
  --top-fractions 1.0 0.5 0.25 0.1 \
  --fixed-recipes L3 L6
```

## Outputs

The tool writes:

- `analysis_summary.json`
- `ranked_trades.parquet`

Default location:

- `<run-dir>/analysis/stage12_counterfactual/`

## How to interpret

Strong evidence that Stage 3 is the main problem:

- oracle selected-side subset is positive
- fixed `L3` or fixed `L6` improves materially as confidence rises

Strong evidence that Stage 3 is not the only problem:

- oracle selected-side subsets remain negative
- fixed `L3` and `L6` stay negative even at high-confidence cutoffs

If oracle improves but fixed recipes do not:

- Stage 1 and Stage 2 are supplying real directional edge
- Stage 3 recipe target/view redesign is the correct next batch

# Stage1+Stage2 Confidence Execution Policy

This is the focused fixed-execution policy batch that follows the confidence execution analysis.

It narrows the search to:

- fixed `L3` / `L6`
- tighter fractions such as `0.5`, `0.33`, `0.25`
- optional side caps to trim an over-dominant side after confidence ranking

## Why this exists

The confidence execution run showed:

- broad execution is still weak
- tighter fractions can become positive on holdout
- the remaining structural issue is side concentration, especially PE-heavy books

This tool answers the next question:

- can a validation-selected fixed execution policy stay positive while reducing one-sided exposure?

## What it does

For one completed staged run it:

- reloads the saved Stage 1 and Stage 2 packages
- replays Stage 1 and Stage 2 on:
  - `research_valid`
  - `final_holdout`
- ranks surviving trades by:
  - `entry_prob * direction_trade_prob * selected_side_prob`
- searches over:
  - fixed recipes such as `L3`, `L6`
  - top fractions such as `0.5`, `0.33`, `0.25`
  - side caps such as `1.0`, `0.85`, `0.75`, `0.70`
- applies the same selected fraction on holdout
- optionally trims the dominant side inside that slice to satisfy the side cap
- ranks candidates on validation using:
  - non-negative return preference
  - profit factor preference
  - side-balance preference
  - minimum trade count preference

## Run

```bash
cd ~/option_trading
. .venv/bin/activate

python -m ml_pipeline_2.run_stage12_confidence_execution_policy \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --top-fractions 0.5 0.33 0.25 \
  --fixed-recipes L3 L6 \
  --side-cap-grid 1.0 0.85 0.75 0.70 \
  --validation-min-trades-soft 50 \
  --side-share-min 0.30 \
  --side-share-max 0.70 \
  --prefer-profit-factor-min 1.0
```

## Output

Default output directory:

- `<run-dir>/analysis/stage12_confidence_execution_policy/`

Artifacts:

- `execution_policy_summary.json`

## Interpretation

Good sign:

- validation picks a tighter fraction
- validation stays near break-even or positive
- holdout stays positive
- side concentration improves materially

Bad sign:

- only uncapped candidates win
- side-capped candidates become too sparse
- validation and holdout still disagree sharply

If this still fails, the next step is not another execution-policy tweak.
Move to:

- explicit side-aware execution logic
- or Stage 3 target/view redesign

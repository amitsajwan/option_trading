# Stage1+Stage2 Confidence Execution

This is the follow-up to the Stage1+Stage2 counterfactual analysis.

It turns the counterfactual into a proper research selector:

- choose a confidence cutoff on `research_valid`
- choose a fixed recipe on `research_valid`
- apply the same validation-selected fraction to `final_holdout`
- report the holdout result

This avoids picking the top fraction directly on holdout while also avoiding raw-score calibration drift between validation and holdout.

## What it does

For one completed staged run it:

- reloads the saved Stage 1 and Stage 2 packages
- replays Stage 1 and Stage 2 on:
  - `research_valid`
  - `final_holdout`
- ranks surviving trades by:
  - `entry_prob * direction_trade_prob * selected_side_prob`
- searches:
  - top fractions such as `1.0`, `0.5`, `0.33`, `0.25`, `0.1`
  - fixed recipes such as `L3`, `L6`
- ranks candidates on validation with soft preferences for:
  - non-negative return
  - profit factor
  - side balance
  - enough trades
- then evaluates the chosen candidate on holdout using the same validation-selected fraction

## Why this exists

The counterfactual proved:

- Stage 1 + Stage 2 contain real edge
- fixed `L3` / `L6` can become positive on high-confidence subsets
- the broad 285-trade set is too weak

This tool answers the next question:

- can a validation-selected confidence gate plus fixed recipe produce a defensible holdout result?

## Run

```bash
cd ~/option_trading
. .venv/bin/activate

python -m ml_pipeline_2.run_stage12_confidence_execution \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --top-fractions 1.0 0.5 0.33 0.25 0.1 \
  --fixed-recipes L3 L6 \
  --transfer-mode fraction \
  --validation-min-trades-soft 50 \
  --side-share-min 0.30 \
  --side-share-max 0.70 \
  --prefer-profit-factor-min 1.0
```

## Outputs

Default output directory:

- `<run-dir>/analysis/stage12_confidence_execution/`

Artifacts:

- `execution_summary.json`
- `ranked_trades_valid.parquet`
- `ranked_trades_holdout.parquet`

## Interpretation

Good sign:

- validation picks a tighter fraction
- chosen recipe is stable
- holdout stays positive with sensible trade count

Bad sign:

- only ultra-small fractions work
- holdout still fails after applying the same validation-selected fraction
- side balance remains structurally broken

If this still fails, the next step is not another confidence-gating tweak.
Move to:

- Stage 3 recipe target/view redesign
- or explicit side-balanced execution logic on top of the current Stage1+Stage2 path

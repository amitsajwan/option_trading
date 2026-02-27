# Strategy Comparison Spec V1 (T10)

This harness compares three execution policies on the same dataset/folds:

1. `ce_only`
2. `pe_only`
3. `dual` (CE/PE with no-trade when both thresholds fail)

## Fairness Contract

All policies must use:

1. same labeled input rows
2. same walk-forward fold configuration
3. same model training config
4. same thresholds (except disabled side in single-side modes)

If test rows/folds differ across modes, comparison is invalid and execution fails.

## Policy Definitions

Given optimized thresholds `ce_threshold`, `pe_threshold`:

- `ce_only`: use `ce_threshold`, disable PE (`pe_threshold=2.0`)
- `pe_only`: use `pe_threshold`, disable CE (`ce_threshold=2.0`)
- `dual`: use both thresholds and choose stronger side when both trigger

## Cost Sensitivity

Supports a cost grid (example: `default,0.001,0.002`) to show fee/slippage impact.

## Outputs

- per-mode, per-cost summaries:
  - trade count
  - trade rate
  - CE/PE split
  - gross/net return sums
  - mean net return per trade
  - win rate
  - max drawdown
- ranking at default cost
- consistency check (`test_rows_total`, `fold_count`)

Artifact:

- `ml_pipeline/artifacts/t10_strategy_comparison_report.json`

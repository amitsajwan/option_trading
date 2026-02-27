# Backtest Spec V1 (T09)

Backtest engine executes threshold-based CE/PE trade decisions on fold test partitions.

## Inputs

1. Labeled dataset (`t05_labeled_features.parquet`)
2. Threshold report (`t08_threshold_report.json`)

## Execution Logic

For each walk-forward fold:

1. Train CE and PE models using fold train days only.
2. Score probabilities on fold test rows.
3. Decision rule per row:
   - if `ce_prob >= ce_threshold` and `pe_prob < pe_threshold` -> CE
   - if `pe_prob >= pe_threshold` and `ce_prob < ce_threshold` -> PE
   - if both pass -> side with higher probability
   - else -> no trade

## Trade Accounting

For selected side:

- `gross_return = <side>_forward_return`
- `net_return = gross_return - cost_per_trade`
- `entry_timestamp = decision_timestamp + 1 minute`
- `exit_timestamp = decision_timestamp + horizon_minutes`

## Outputs

1. Event-level trades parquet
2. Backtest summary JSON with:
   - trade count, side split, win rate
   - gross/net return sums
   - mean net return per trade
   - max drawdown on cumulative net-return curve
   - per-fold summary

Artifacts:

- `ml_pipeline/artifacts/t09_backtest_trades.parquet`
- `ml_pipeline/artifacts/t09_backtest_report.json`

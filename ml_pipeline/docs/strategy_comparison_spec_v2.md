# Strategy Comparison Spec V2 (T19)

T19 compares fixed-horizon exits versus dynamic exit-policy profiles on identical evaluation windows.

## Objective

Evaluate policy families without changing folds/test rows.

## Profiles

Default profiles:

1. `fixed_horizon`
2. `path_v2_default`
3. `path_v2_best_t18` (included when T18 report exists and differs from default)

## Data Integrity Rule

All compared profiles must match:

- `test_rows_total`
- `fold_count`

If not, comparison fails.

## Ranking

Profiles are ranked by:

1. `net_return_sum`
2. `mean_net_return_per_trade`
3. lower `slippage_per_trade` preference

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.strategy_comparison_v2 --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t18-report ml_pipeline\artifacts\t18_exit_policy_optimization_report.json --report-out ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json
```

## Artifact

- `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`

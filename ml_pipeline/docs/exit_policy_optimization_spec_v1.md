# Exit Policy Optimization Spec V1 (T18)

T18 adds a deterministic search harness over Backtest V2 policy parameters.

## Objective

Compare exit-policy configurations on identical fold/evaluation windows and select the best net outcome.

## Search Dimensions

Current grid (configurable via CLI):

1. `intrabar_tie_break` (`sl`, `tp`)
2. `slippage_per_trade` (float grid)
3. `forced_eod_exit_time` (HH:MM grid)

Execution mode is fixed to `path_v2`.

## Selection Logic

Ranking key (descending):

1. `net_return_sum`
2. `mean_net_return_per_trade`
3. lower `slippage_per_trade` preference
4. deterministic tie-break on config ordering

## Consistency Constraint

All candidate configurations must evaluate on the same:

- `test_rows_total`
- `fold_count`

Mismatch triggers failure.

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.exit_policy_optimization --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --tie-break-grid sl,tp --slippage-grid 0.0,0.0002,0.0005 --forced-eod-grid 15:24 --report-out ml_pipeline\artifacts\t18_exit_policy_optimization_report.json
```

## Artifact

- `ml_pipeline/artifacts/t18_exit_policy_optimization_report.json`

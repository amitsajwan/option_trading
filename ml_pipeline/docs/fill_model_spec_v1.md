# Fill Model Spec V1 (T20)

T20 adds configurable slippage/fill modeling to Backtest V2.

## Supported Models

1. `constant`
   - uses `constant_slippage`
2. `spread_fraction`
   - slippage = `spread_fraction * ((high-low)/close)`
3. `liquidity_adjusted`
   - slippage = `spread_fraction * spread_proxy + volume_impact_coeff / sqrt(volume)`

All models are clamped to `[min_slippage, max_slippage]`.

## Integration

Backtest now computes:

- `slippage_model_component` (from fill model)
- `slippage_total = slippage_per_trade + slippage_model_component`
- `net_return = gross_return - cost_per_trade - slippage_total`

## CLI Example

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --execution-mode path_v2 --fill-model liquidity_adjusted --fill-spread-fraction 0.5 --fill-volume-impact 0.02 --fill-min 0.0 --fill-max 0.01 --slippage-per-trade 0.0002 --trades-out ml_pipeline\artifacts\t20_backtest_trades.parquet --report-out ml_pipeline\artifacts\t20_backtest_report.json
```

## Artifacts

- `ml_pipeline/artifacts/t20_backtest_trades.parquet`
- `ml_pipeline/artifacts/t20_backtest_report.json`

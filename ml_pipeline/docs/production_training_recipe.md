# Production Training Recipe

This recipe captures the current production-oriented setup:

- Label horizon: `15` minutes
- TP/SL path: `+30% / -20%` on option premium
- Training target: `path_tp_sl` (time-stop rows excluded from target)
- Objective: `trade_utility` (net return with PF/DD/trade-count constraints)
- Extra unseen holdout: reserve recent `2-3` months

## 1) Re-label with production path spec

```powershell
python -m ml_pipeline.label_engine `
  --features ml_pipeline/artifacts/t04_features.parquet `
  --base-path C:\Users\amits\Downloads\archive\banknifty_data `
  --horizon-minutes 15 `
  --stop-loss-pct 0.20 `
  --take-profit-pct 0.30 `
  --out ml_pipeline/artifacts/t05_labeled_features.parquet `
  --report-out ml_pipeline/artifacts/t05_label_report.json `
  --path-report-out ml_pipeline/artifacts/t15_label_path_report.json
```

Notes:

- `ce_path_target_valid` / `pe_path_target_valid` are emitted for deterministic TP/SL-only target filtering.

## 2) Train with utility objective and reserved unseen holdout

```powershell
python -m ml_pipeline.train_two_year_pipeline `
  --base-path C:\Users\amits\Downloads\archive\banknifty_data `
  --lookback-years 2 `
  --reserve-months 3 `
  --objective trade_utility `
  --feature-profile core_v1 `
  --label-target path_tp_sl `
  --label-horizon-minutes 15 `
  --label-stop-loss-pct 0.20 `
  --label-take-profit-pct 0.30 `
  --utility-ce-threshold 0.60 `
  --utility-pe-threshold 0.60 `
  --utility-min-profit-factor 1.30 `
  --utility-max-abs-drawdown 0.15 `
  --utility-min-trades 50 `
  --artifact-prefix t38_prod
```

Artifacts include:

- model/report outputs
- `*_holdout_days.json` for strict out-of-time evaluation

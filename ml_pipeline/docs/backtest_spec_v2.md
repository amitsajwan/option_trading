# Backtest Spec V2 (T16): Intrabar Exit Simulation

T16 extends the backtest engine with explicit execution semantics and exit reasons.

## Modes

1. `fixed_horizon` (default, backward-compatible)
   - Uses `*_forward_return` from label horizon.
   - Exit is always time-based at `t+H`.
2. `path_v2`
   - Uses path-aware label columns from T15:
     - `*_path_exit_reason`
     - `*_tp_price`
     - `*_sl_price`
     - `*_first_hit_offset_min`
   - Supports deterministic intrabar tie-break for `tp_sl_same_bar`.

## Exit Reasons

Trade output column: `exit_reason` in:

- `tp`
- `sl`
- `time`
- `trail`
- `forced_eod`

## Deterministic Intrabar Rule

If both TP and SL are hit in same minute bar (`tp_sl_same_bar`), resolve via:

- `--intrabar-tie-break sl` (default)
- `--intrabar-tie-break tp`

## Costs

Net return is:

`net_return = gross_return - cost_per_trade - slippage_per_trade`

Both cost and slippage are applied per trade.

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --execution-mode path_v2 --intrabar-tie-break sl --slippage-per-trade 0.0002 --trades-out ml_pipeline\artifacts\t16_backtest_trades.parquet --report-out ml_pipeline\artifacts\t16_backtest_report.json
```

## Outputs

- Event-level trades parquet with:
  - `execution_mode`
  - `path_exit_reason`
  - `exit_reason`
  - `slippage_per_trade`
- Summary report with:
  - `execution_mode`
  - `intrabar_tie_break`
  - `exit_reason_counts`

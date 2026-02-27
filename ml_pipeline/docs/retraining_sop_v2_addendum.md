# Retraining SOP V2 Addendum (T24)

This addendum extends `retraining_sop_v1.md` for the exit-aware Phase-2 stack.

## 1. Additional Retraining Triggers

Run Phase-2 retraining/re-evaluation when:

- T23 raises execution-drift alerts.
- Fill-cost assumptions change materially.
- Exit-policy defaults (`intrabar_tie_break`, forced EOD time, hold limits) are revised.

## 2. Required Inputs

- All V1 inputs/artifacts.
- Exit-policy config assumptions from T14.
- Phase-2 evaluation contracts:
  - `t18_exit_policy_optimization_report.json`
  - `t19_strategy_comparison_v2_report.json`
  - `t20_backtest_report.json`
  - `t21_replay_evaluation_report.json`

## 3. Phase-2 Procedure

After finishing V1 retraining (T01-T13), execute:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.exit_policy --report-out ml_pipeline\artifacts\t14_exit_policy_validation_report.json --normalized-out ml_pipeline\artifacts\t14_exit_policy_config.json
python -m ml_pipeline.label_engine --features ml_pipeline\artifacts\t04_features.parquet --base-path C:\Users\amits\Downloads\archive\banknifty_data --out ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t05_label_report.json --path-report-out ml_pipeline\artifacts\t15_label_path_report.json --stop-loss-pct 0.12 --take-profit-pct 0.24
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --execution-mode path_v2 --intrabar-tie-break sl --slippage-per-trade 0.0002 --trades-out ml_pipeline\artifacts\t16_backtest_trades.parquet --report-out ml_pipeline\artifacts\t16_backtest_report.json
python -m ml_pipeline.dynamic_exit_policy --out ml_pipeline\artifacts\t17_dynamic_exit_policy_report.json
python -m ml_pipeline.exit_policy_optimization --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --tie-break-grid sl,tp --slippage-grid 0.0,0.0002,0.0005 --forced-eod-grid 15:24 --report-out ml_pipeline\artifacts\t18_exit_policy_optimization_report.json
python -m ml_pipeline.strategy_comparison_v2 --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t18-report ml_pipeline\artifacts\t18_exit_policy_optimization_report.json --report-out ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --execution-mode path_v2 --fill-model liquidity_adjusted --fill-spread-fraction 0.5 --fill-volume-impact 0.02 --fill-min 0.0 --fill-max 0.01 --slippage-per-trade 0.0002 --trades-out ml_pipeline\artifacts\t20_backtest_trades.parquet --report-out ml_pipeline\artifacts\t20_backtest_report.json
python -m ml_pipeline.paper_replay_evaluation --decisions-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t19-report ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json --trades-out ml_pipeline\artifacts\t21_replay_evaluation_trades.parquet --report-out ml_pipeline\artifacts\t21_replay_evaluation_report.json
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --limit 300 --max-hold-minutes 5 --confidence-buffer 0.05
python -m ml_pipeline.monitoring_execution --reference-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --current-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --report-out ml_pipeline\artifacts\t23_execution_monitoring_report.json --summary-out ml_pipeline\artifacts\t23_execution_monitoring_summary.md
```

## 4. Promotion Gate Additions (V2)

Require all of the following:

1. `t18` profile optimization report generated with deterministic ranking.
2. `t19` profile comparison confirms selected profile on identical test rows.
3. `t20` stress run reviewed and accepted for configured fill/slippage assumptions.
4. `t21` replay evaluation has acceptable match rate and expected-value behavior.
5. `t23` execution monitoring status is `ok` or reviewed warnings only.

## 5. Rollback Guidance (V2)

- Rollback model/threshold/profile as a tuple:
  - `t06_baseline_model.joblib`
  - `t08_threshold_report.json`
  - selected `t19` profile parameters
- If execution drift persists after rollback, disable managed exits and revert to fixed-horizon paper mode for diagnosis.

## 6. Phase 3 Retrain/Eval Extension (T32-T34)

After retraining and threshold selection, run:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.diagnostics_stress --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --model-package ml_pipeline\artifacts\t29_2y_auto_best_model.joblib --threshold-report ml_pipeline\artifacts\t31_calibration_threshold_report.json --report-out ml_pipeline\artifacts\t32_diagnostics_stress_report.json --summary-out ml_pipeline\artifacts\t32_diagnostics_stress_summary.md
python -m ml_pipeline.order_intent_runtime --decisions-jsonl ml_pipeline\artifacts\t33_paper_capital_events_actual.jsonl --report-out ml_pipeline\artifacts\t33_order_runtime_report.json --summary-out ml_pipeline\artifacts\t33_order_runtime_summary.md --intents-out ml_pipeline\artifacts\t33_order_intents.parquet --fills-out ml_pipeline\artifacts\t33_order_fills.parquet
python -m ml_pipeline.phase3_reproducibility_runner --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --model-package ml_pipeline\artifacts\t29_2y_auto_best_model.joblib --threshold-report ml_pipeline\artifacts\t31_calibration_threshold_report.json --decisions-jsonl ml_pipeline\artifacts\t33_paper_capital_events_actual.jsonl --workdir ml_pipeline\artifacts\t34_phase3_reproducibility --report-out ml_pipeline\artifacts\t34_phase3_reproducibility_report.json --summary-out ml_pipeline\artifacts\t34_phase3_reproducibility_summary.md
```

Phase 3 promotion gates:

1. T32 report reviewed; no unacceptable train-valid generalization gaps for active side.
2. T33 reconciliation has `unmatched_intents=0`, `side_mismatch=0`, and `kind_mismatch=0`.
3. T33 `runtime_guards.kill_switch = false`.
4. T34 reproducibility `status = pass` with zero mismatches.

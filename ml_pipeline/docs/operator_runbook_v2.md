# Operator Runbook V2 (T24)

This runbook extends V1 for the exit-aware Phase-2 stack (T14-T23).

## 1. Preconditions

- Python environment has `ml_pipeline/requirements-ml.txt` installed.
- Archive base is available (`LOCAL_HISTORICAL_BASE` or `--base-path`).
- Phase-1 core artifacts are present:
  - `ml_pipeline/artifacts/t04_features.parquet`
  - `ml_pipeline/artifacts/t06_baseline_model.joblib`
  - `ml_pipeline/artifacts/t08_threshold_report.json`
- Phase-2 optimization artifact is present:
  - `ml_pipeline/artifacts/t18_exit_policy_optimization_report.json`

## 2. Start-of-Day Checklist

1. Verify previous execution monitoring report:
   - `ml_pipeline/artifacts/t23_execution_monitoring_report.json`
   - `status` should be `ok`; review alerts before enabling paper replay.
2. Confirm selected Phase-2 profile from:
   - `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`
   - use `best_profile` as active execution profile.
3. Run a short exit-aware replay smoke check:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --limit 100 --max-hold-minutes 5 --confidence-buffer 0.05
```

## 3. Intraday Paper Operations (Exit-Aware)

1. Generate exit-aware event stream (ENTRY/MANAGE/EXIT/IDLE):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --limit 300 --max-hold-minutes 5 --confidence-buffer 0.05
```

2. Evaluate realized outcomes against labeled data:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.paper_replay_evaluation --decisions-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t19-report ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json --trades-out ml_pipeline\artifacts\t21_replay_evaluation_trades.parquet --report-out ml_pipeline\artifacts\t21_replay_evaluation_report.json
```

3. Run execution-quality drift monitoring:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.monitoring_execution --reference-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --current-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --report-out ml_pipeline\artifacts\t23_execution_monitoring_report.json --summary-out ml_pipeline\artifacts\t23_execution_monitoring_summary.md
```

## 4. Alert Policy

- `event_type_distribution.max_shift >= 0.15`: investigate event-mix drift.
- `exit_reason_distribution.max_shift >= 0.15`: investigate exit policy drift.
- `hold_duration.mean_shift >= 2.0` or `hold_duration.p95_shift >= 3.0`: investigate hold-time regime shift.

If any alert triggers:

1. Keep system in paper-only mode.
2. Run `retraining_sop_v2_addendum.md`.
3. Re-run T19/T20/T21 before restoring active profile.

## 5. End-of-Day

1. Archive:
   - `t22_exit_aware_paper_events.jsonl`
   - `t21_replay_evaluation_report.json`
   - `t23_execution_monitoring_report.json`
2. Record:
   - profile name/config used
   - event-type distribution
   - exit-reason distribution
   - any alert incidents and mitigation

## 6. Phase 3 Runtime Controls (T32-T34)

Use these checks before any shadow/live promotion:

1. Re-run overfit/underfit and cost-slippage stress:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.diagnostics_stress --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --model-package ml_pipeline\artifacts\t29_2y_auto_best_model.joblib --threshold-report ml_pipeline\artifacts\t31_calibration_threshold_report.json --report-out ml_pipeline\artifacts\t32_diagnostics_stress_report.json --summary-out ml_pipeline\artifacts\t32_diagnostics_stress_summary.md
```

2. Build idempotent intents + reconciliation + guard status from latest paper events:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.order_intent_runtime --decisions-jsonl ml_pipeline\artifacts\t33_paper_capital_events_actual.jsonl --report-out ml_pipeline\artifacts\t33_order_runtime_report.json --summary-out ml_pipeline\artifacts\t33_order_runtime_summary.md --intents-out ml_pipeline\artifacts\t33_order_intents.parquet --fills-out ml_pipeline\artifacts\t33_order_fills.parquet
```

3. Run Phase 3 reproducibility check (clean run1/run2 compare):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.phase3_reproducibility_runner --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --model-package ml_pipeline\artifacts\t29_2y_auto_best_model.joblib --threshold-report ml_pipeline\artifacts\t31_calibration_threshold_report.json --decisions-jsonl ml_pipeline\artifacts\t33_paper_capital_events_actual.jsonl --workdir ml_pipeline\artifacts\t34_phase3_reproducibility --report-out ml_pipeline\artifacts\t34_phase3_reproducibility_report.json --summary-out ml_pipeline\artifacts\t34_phase3_reproducibility_summary.md
```

Promotion gate:

- `t33_order_runtime_report.json.runtime_guards.kill_switch` must be `false` for promotion.
- `t34_phase3_reproducibility_report.json.status` must be `pass`.

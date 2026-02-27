# Retraining SOP V1 (T13)

> Status: Historical (`v1`).
> Current active SOP: `ml_pipeline/docs/retraining_sop_v2_addendum.md`.

This SOP defines the controlled retraining cycle for the ML timing model.

## 1. Retraining Triggers

Start retraining when one or more conditions hold:

- Scheduled cadence reached (weekly or bi-weekly).
- T12 drift monitor raises `high` severity.
- Data contract changes (schema/features/label parameters).
- Material performance degradation in paper metrics.

## 2. Inputs Required

- Archive base path with futures/spot/options minute data.
- Stable config values:
  - label config (`horizon`, return threshold, excursion gate)
  - train config (`max_depth`, `n_estimators`, `learning_rate`, `random_state`)
  - decision config (threshold search grid, cost assumptions)
- Prior model artifacts for side-by-side comparison.

## 3. Procedure

1. Validate input contract:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.schema_validator
```

2. Rebuild quality + dataset + features + labels:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.quality_profiler
python -m ml_pipeline.dataset_builder
python -m ml_pipeline.feature_engineering --panel ml_pipeline\artifacts\t03_canonical_panel.parquet --out ml_pipeline\artifacts\t04_features.parquet
python -m ml_pipeline.label_engine --features ml_pipeline\artifacts\t04_features.parquet --out ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t05_label_report.json
```

3. Re-train and evaluate:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.train_baseline --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --model-out ml_pipeline\artifacts\t06_baseline_model.joblib --report-out ml_pipeline\artifacts\t06_train_report.json
python -m ml_pipeline.walk_forward --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t07_walk_forward_report.json --train-days 3 --valid-days 1 --test-days 1 --step-days 1
python -m ml_pipeline.threshold_optimization --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t08_threshold_report.json --train-days 3 --valid-days 1 --test-days 1 --step-days 1
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --trades-out ml_pipeline\artifacts\t09_backtest_trades.parquet --report-out ml_pipeline\artifacts\t09_backtest_report.json
python -m ml_pipeline.strategy_comparison --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --report-out ml_pipeline\artifacts\t10_strategy_comparison_report.json --cost-grid default,0.001,0.002
```

4. Paper replay + drift baseline:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --limit 200
python -m ml_pipeline.monitoring_drift --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --reference-features ml_pipeline\artifacts\t04_features.parquet --current-features ml_pipeline\artifacts\t04_features.parquet --current-decisions ml_pipeline\artifacts\t11_paper_decisions.jsonl --report-out ml_pipeline\artifacts\t12_drift_report.json --summary-out ml_pipeline\artifacts\t12_drift_summary.md
```

## 4. Promotion Gate

Promote only if all pass:

1. Tests pass (`python -m unittest discover -s ml_pipeline/tests -v`).
2. Walk-forward and backtest are complete with no leakage errors.
3. Strategy comparison report generated and reviewed.
4. Drift baseline generated with acceptable status.
5. Model card + runbook metadata updated (version/date/config).

## 5. Rollback Plan

- Keep previous `t06` and `t08` artifacts versioned.
- If new model underperforms in paper mode:
  - revert model+threshold pair together
  - log incident and retraining delta analysis

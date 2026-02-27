# Operator Runbook V1 (T13)

> Status: Historical (`v1`).
> Current active runbook: `ml_pipeline/docs/operator_runbook_v2.md`.

This runbook defines day-to-day operating steps for paper/live-model timing decisions.

## 1. Preconditions

- Python environment has `ml_pipeline/requirements-ml.txt` installed.
- Archive base exists at `LOCAL_HISTORICAL_BASE` (or passed via `--base-path`).
- Required artifacts are present:
  - `ml_pipeline/artifacts/t06_baseline_model.joblib`
  - `ml_pipeline/artifacts/t08_threshold_report.json`
- API endpoints are reachable for live polling mode:
  - Market API: `http://127.0.0.1:8004`
  - Dashboard API: `http://127.0.0.1:8002`

## 2. Start-of-Day Checklist

1. Confirm previous-day drift status from `ml_pipeline/artifacts/t12_drift_summary.md`.
2. Confirm threshold report and model package timestamps are from same model cycle.
3. Replay smoke-check:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --limit 100
```

## 3. Intraday Operation (Paper Mode)

### Replay-dry-run

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --limit 200
```

### Live-api polling

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-api --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --market-api-base http://127.0.0.1:8004 --dashboard-api-base http://127.0.0.1:8002 --output-jsonl ml_pipeline\artifacts\t11_live_paper_decisions.jsonl --poll-seconds 5 --max-iterations 60
```

## 4. Drift Monitoring

Run at least once per session (or after large regime move):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.monitoring_drift --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --reference-features ml_pipeline\artifacts\t04_features.parquet --current-features ml_pipeline\artifacts\t04_features.parquet --current-decisions ml_pipeline\artifacts\t11_paper_decisions.jsonl --report-out ml_pipeline\artifacts\t12_drift_report.json --summary-out ml_pipeline\artifacts\t12_drift_summary.md
```

Alert policy:

- `warn`: feature PSI >= 0.10
- `high`: feature PSI >= 0.20
- `high`: CE/PE prediction PSI >= 0.20
- `high`: action-share shift >= 0.15

If any `high` alert triggers, freeze promotions and run retraining SOP.

## 5. Incident Playbook

1. `No decisions emitted`:
   - Check output JSONL write permissions.
   - Check feature row completeness and model package schema.
2. `API polling failure`:
   - Validate endpoints; retry with replay mode.
3. `Drift high alerts`:
   - Move to paper-only mode.
   - Execute retraining SOP and compare against prior model.

## 6. End-of-Day

1. Archive:
   - decisions JSONL
   - drift report/summary
   - any retraining outputs
2. Record:
   - model version
   - threshold report version
   - drift status
3. Open issues for data anomalies before next session.

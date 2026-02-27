# Execution Monitoring Spec V1 (T23)

T23 adds execution-quality drift monitoring for exit-aware paper events.

## Inputs

- Reference events JSONL (baseline)
- Current events JSONL

Both are expected to contain:

- `event_type`
- `event_reason`
- `held_minutes` (for exits/manages where available)

## Drift Checks

1. Event-type distribution shift (`ENTRY/MANAGE/EXIT/IDLE`)
2. Exit-reason distribution shift (subset on `EXIT`)
3. Hold-duration shifts (`mean`, `p95`)

## Alerts

Default high-alert thresholds:

- event share max shift >= `0.15`
- exit reason share max shift >= `0.15`
- hold mean shift >= `2.0`
- hold p95 shift >= `3.0`

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.monitoring_execution --reference-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --current-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --report-out ml_pipeline\artifacts\t23_execution_monitoring_report.json --summary-out ml_pipeline\artifacts\t23_execution_monitoring_summary.md
```

## Artifacts

- `ml_pipeline/artifacts/t23_execution_monitoring_report.json`
- `ml_pipeline/artifacts/t23_execution_monitoring_summary.md`

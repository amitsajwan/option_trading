# Monitoring and Drift Spec V1 (T12)

This stage adds runtime monitoring for:

1. feature distribution drift
2. prediction/action distribution drift

## Inputs

- model package (`t06_baseline_model.joblib`)
- threshold report (`t08_threshold_report.json`)
- reference features parquet
- current features parquet
- current decisions JSONL (from T11)

## Feature Drift

For shared numeric features between reference and current datasets:

- compute PSI (Population Stability Index)
- compute mean shift (in reference std units)

Thresholds:

- warn: `feature_psi_warn` (default `0.10`)
- alert: `feature_psi_alert` (default `0.20`)

## Prediction Drift

Compare reference predicted probabilities vs current decision probabilities:

- `ce_prob` PSI
- `pe_prob` PSI
- action-share max shift (`BUY_CE`, `BUY_PE`, `HOLD`)

Thresholds:

- prediction PSI alert: `0.20`
- action-share shift alert: `0.15`

## Output

1. Detailed drift report JSON
2. Summary markdown
3. Alert list with severity (`warn`/`high`)

Artifacts:

- `ml_pipeline/artifacts/t12_drift_report.json`
- `ml_pipeline/artifacts/t12_drift_summary.md`

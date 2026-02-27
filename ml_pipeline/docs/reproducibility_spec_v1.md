# Reproducibility Spec V1 (T13)

> Status: Historical (`v1`).
> Current active reproducibility flow: `ml_pipeline/docs/reproducibility_spec_v2.md`.

T13 adds a single-command reproducibility flow that re-executes T01-T12 in isolated directories and verifies deterministic equivalence.

## Command

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.reproducibility_runner --base-path C:\Users\amits\Downloads\archive\banknifty_data --workdir ml_pipeline\artifacts\t13_reproducibility --report-out ml_pipeline\artifacts\t13_reproducibility_report.json --summary-out ml_pipeline\artifacts\t13_reproducibility_summary.md
```

## What It Runs

1. T01 schema validation
2. T02 data quality profiling
3. T03 canonical dataset build
4. T04 feature engineering
5. T05 label build
6. T06 training
7. T07 walk-forward
8. T08 threshold optimization
9. T09 backtest
10. T10 strategy comparison
11. T11 paper replay decisions
12. T12 drift report

By default, it runs the full stack twice (`run1`, `run2`) and compares artifact signatures.

## Comparison Rules

- JSON/JSONL: normalized comparison; volatile timestamps are ignored (`created_at_utc`, `generated_at`).
- Parquet: DataFrame hash signature over content + schema.
- Fails if any checked artifact diverges.

## Outputs

- `ml_pipeline/artifacts/t13_reproducibility_report.json`
- `ml_pipeline/artifacts/t13_reproducibility_summary.md`
- `ml_pipeline/artifacts/t13_reproducibility/run1/...`
- `ml_pipeline/artifacts/t13_reproducibility/run2/...`

## CI-Friendly Mode

For a faster single execution without comparison:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.reproducibility_runner --single-run
```

# Reproducibility Spec V2 (T24)

T24 adds a clean-room reproducibility flow for Phase-2 (`T14`-`T23`).

## Command (Clean-Room Recommended)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.phase2_reproducibility_runner --base-path C:\Users\amits\Downloads\archive\banknifty_data --workdir ml_pipeline\artifacts\t24_phase2_reproducibility --bootstrap-phase1 --report-out ml_pipeline\artifacts\t24_phase2_reproducibility_report.json --summary-out ml_pipeline\artifacts\t24_phase2_reproducibility_summary.md
```

`--bootstrap-phase1` runs `reproducibility_runner --single-run` inside the T24 workdir to seed required Phase-1 artifacts (`t04`, `t06`, `t08`, `t11`) before running Phase-2.

## Alternate Mode

If you already have a trusted Phase-1 artifact directory:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.phase2_reproducibility_runner --base-path C:\Users\amits\Downloads\archive\banknifty_data --phase1-artifacts-dir ml_pipeline\artifacts --workdir ml_pipeline\artifacts\t24_phase2_reproducibility --report-out ml_pipeline\artifacts\t24_phase2_reproducibility_report.json --summary-out ml_pipeline\artifacts\t24_phase2_reproducibility_summary.md
```

## What It Runs

Per run (`run1`, `run2`):

1. `T14` exit policy validation
2. `T15` path-aware labels
3. `T16` path-v2 backtest
4. `T17` dynamic exit policy simulation
5. `T18` exit policy optimization
6. `T19` strategy comparison v2
7. `T20` fill/slippage stress backtest
8. `T21` replay evaluation
9. `T22` exit-aware replay events
10. `T23` execution monitoring

Then compares deterministic signatures across key JSON/JSONL/Parquet outputs.

## Determinism Rules

- JSON and JSONL comparisons normalize volatile timestamp keys (`created_at_utc`, `generated_at`).
- Parquet comparison uses content+schema signature hashing.
- Reproducibility passes only when no artifact mismatches are found.

## Outputs

- `ml_pipeline/artifacts/t24_phase2_reproducibility_report.json`
- `ml_pipeline/artifacts/t24_phase2_reproducibility_summary.md`
- `ml_pipeline/artifacts/t24_phase2_reproducibility/run1/...`
- `ml_pipeline/artifacts/t24_phase2_reproducibility/run2/...`

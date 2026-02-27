# Live Inference Spec V1 (T11)

> Status: Historical (`v1`).
> Current active spec: `ml_pipeline/docs/live_inference_spec_v2.md`.

Paper-mode live inference adapter supports two run modes:

1. `replay-dry-run`
2. `live-api`

## Model Inputs

- model package: `t06_baseline_model.joblib`
- threshold report: `t08_threshold_report.json`

## Decision Logic

Per row:

1. Predict `ce_prob`, `pe_prob`
2. Apply mode-specific action policy:
   - `dual`: choose CE/PE or HOLD
   - `ce_only`: CE or HOLD
   - `pe_only`: PE or HOLD

Output action values:

- `BUY_CE`
- `BUY_PE`
- `HOLD`

## Replay Dry-Run (integration path)

- Input: feature parquet (T04 output or compatible)
- Runs deterministic row-by-row inference
- Emits decision JSONL records

## Live API Mode

Consumes current runtime stack endpoints:

- Market API OHLC (`:8004`)
- Dashboard options chain (`:8002`)

Builds latest feature row from recent bars + option-chain snapshot and emits paper decisions.

## Outputs

- decision stream JSONL (append-only)
- summary stats (`decisions_emitted`, action counts)

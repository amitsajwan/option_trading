# Intraday Profit Execution Plan

## Status

Historical research note.

This file previously acted as the active control document for an `S0`-`S5` MIDDAY recovery track.
It is no longer the current operating instruction for `ml_pipeline_2`.

## Why It Is Historical

This note refers to a bounded research phase that used:

- story tracking (`S0` through `S5`)
- older GCP run roots under `artifacts/training_launches/...`
- a specific `S3` redesign line for Stage 2 regime features

Those references are useful as context, but they no longer represent the current package workflow.

## Use Instead

For current package operation and code-aligned behavior, use:

- [README.md](README.md)
- [gcp_user_guide.md](gcp_user_guide.md)
- [architecture.md](architecture.md)
- [detailed_design.md](detailed_design.md)

For active execution state, use the actual persisted artifacts for the run you are investigating:

- `run_status.json`
- `grid_status.json`
- `state.jsonl`
- `summary.json`
- `grid_summary.json`

## What This File Still Provides

This file is retained as evidence of:

- the previous MIDDAY recovery problem framing
- the historical interpretation that Stage 2 direction was the main bottleneck
- the transition from threshold tuning toward feature redesign and scenario testing

Do not treat the story tracker, run roots, or operator commands from the older version of this file as current instructions.

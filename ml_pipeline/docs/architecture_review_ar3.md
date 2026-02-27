# Architecture Review AR3 (Post T11)

Date: `2026-02-21`
Scope checkpoint: after T11 (paper-mode live inference adapter)

## Reviewed Areas

1. Runtime adapter boundaries and failure modes
2. Latency/operational behavior in paper mode
3. Output observability and integration path to execution layer

## Findings

1. Adapter now supports two clear modes:
   - `replay-dry-run` for deterministic integration validation
   - `live-api` for runtime polling from current market stack
2. Decision output is append-only JSONL, which is simple and auditable.
3. Inference uses the same model package and thresholds produced by T06/T08, preserving train-serve consistency.
4. Live feature assembly is partial by design (missing fields are handled via model imputer), which is acceptable for paper-mode but must be improved before live execution.

## Decisions

1. Keep paper adapter separate from execution engine.
2. Treat `replay-dry-run` as mandatory preflight before live polling runs.
3. Keep JSONL output contract for downstream monitoring and drift analysis (T12).

## Refactor Actions

No blocking refactor required now.

Accepted hardening changes:

1. Added single-class fold fallback in backtest/training-adjacent runtime paths.
2. Added deterministic replay integration test for adapter decisions.

## Risks for T12/T13

1. Live API payload schema variability may cause missing-feature spikes.
2. Need runtime monitoring on action distribution drift and data freshness.
3. Need explicit operator runbook for restart/recovery and endpoint health dependencies.

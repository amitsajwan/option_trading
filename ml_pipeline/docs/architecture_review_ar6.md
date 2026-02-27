# Architecture Review AR6 (Post T24)

Date: `2026-02-22`  
Scope checkpoint: after T24 (documentation/runbook/reproducibility finalization)

## Reviewed Areas

1. Operational readiness of exit-aware runtime contracts
2. Reproducibility guarantees for Phase-2 stack
3. Documentation completeness for operator and retraining workflows

## Findings

1. Phase-2 runtime contracts are explicit and test-covered:
   - exit policy config validation
   - path-aware backtest exits
   - exit-aware paper event stream
   - execution drift metrics
2. T24 reproducibility flow now supports clean-room bootstrap of Phase-1 prerequisites via `--bootstrap-phase1`.
3. V2 documentation is complete for operations and lifecycle:
   - runbook v2
   - model card addendum
   - retraining SOP addendum
   - reproducibility spec v2

## Decisions

1. Keep Phase-1 and Phase-2 reproducibility runners separate:
   - clear ownership by phase
   - lower refactor risk for stable V1 flows
2. Preserve deterministic artifact-signature checks as release gate.
3. Require profile+model+threshold tuple versioning for any promotion.

## Refactor Actions

No blocking refactor required at AR6 gate.

Accepted follow-up improvements:

1. Add optional profile-conditioned drift baselines by market regime bucket.
2. Add periodic replay/backtest parity report that auto-links T19/T21/T23 artifacts.
3. Add runtime configuration registry for profile promotion history.

## Risks Beyond Phase 2

1. Regime shifts can invalidate both entry model and exit policy simultaneously.
2. Production execution latency/slippage may diverge from replay assumptions.
3. Current evaluation horizon is still single-instrument and may overfit session-specific behavior.

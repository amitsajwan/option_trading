# MIDDAY Recovery Handover

This is the onboarding and takeover document for the staged MIDDAY recovery track in `ml_pipeline_2`.

Use this document to understand:

- what this track tried to solve
- what has already been learned
- what is currently running
- what should not be reopened

For the active control flow, story status, and operator instructions, use [intraday_profit_execution_plan.md](intraday_profit_execution_plan.md).

## What this track is

This recovery line focused on the `MIDDAY` slice as a bounded research wedge for a larger intraday trading product.

The key practical question was:

- can the staged stack produce a publishable directional system that remains economically positive after cost?

The stack under study is:

1. Stage 1 - entry gate
2. Stage 2 - direction gate
3. Stage 3 - execution recipe / policy behavior

## What is now settled

These conclusions should be treated as closed unless new evidence contradicts them:

- Stage 1 is not the main bottleneck in this track.
- The raw oracle is not the main source of skew.
- Stage 2 is the main distortion source.
- Threshold and wrapper approaches are exhausted:
  - symmetric thresholds
  - asymmetric thresholds
  - dual-side fractions
  - side caps
  - manual Stage 2 overrides
- `S2` feature-signal analysis is closed with a `NO` result for retraining as-is.

## Current best interpretation

Stage 2 behaves like a regime amplifier rather than a robust direction detector.

What that means:

- it can overfit the directional dominance of one window
- it does not hold stable CE/PE separation across windows
- retraining the same feature set is not justified

That is why the work moved into `S3` feature redesign.

## Current active work

Current as of `2026-04-11`:

- `S0 DONE`
- `S1 DONE`
- `S2 DONE`
- `S3 IN_PROGRESS`
- `S4/S5 PENDING`

### Active S3 run

The active GCP run root is:

```text
ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/
```

Baseline run:

```text
ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline
```

Current grid behavior:

- `s3_regime_baseline` trains Stage 1 fresh
- `s3_regime_balanced` reuses Stage 1 from baseline

This supersedes the older `stage3_midday_policy_paths_v1` path as the active workstream.

## What changed in S3

The approved redesign adds regime-context features to Stage 2.

Implemented changes:

- `compute_rolling_oracle_stats()` in `staged/pipeline.py`
- Stage 2 frame enrichment with `oracle_rolling_*`
- `fo_midday_direction_regime_v1` in `catalog/feature_sets.py`
- `staged_grid.stage3_direction_regime_v1.json`

The purpose is to test whether recent oracle regime context helps Stage 2 stop projecting one window's dominance into the next.

## What not to reopen

Do not restart these as primary solution paths:

- threshold-only tuning
- side-cap tuning
- PE-only subset framing as product evidence
- generic wrapper experimentation without changing Stage 2 signal

Those paths are now historical evidence, not active direction.

## Morning checklist for a new engineer

1. Read [intraday_profit_execution_plan.md](intraday_profit_execution_plan.md).
2. Check the active `S3` run status under:
   - `ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/`
3. If baseline run completed, run:
   - `run_stage2_feature_signal_diagnostic`
   - `run_stage12_skew_diagnostic`
   - `run_stage12_confidence_execution_policy`
4. Deliver raw outputs to `CORE`.
5. Do not improvise new model branches until `CORE` reviews the results.

## Live inference gap

Known `S4` dependency:

- `oracle_rolling_*` is a training-time feature family derived from historical oracle outcomes
- live `ml_pure` inference will need a serving-time lookup/input path for those values

This is deferred to `S4`.
It should not block the active `S3` run.

## Historical references

These documents are still useful for background, but they are not the active control docs:

- [research_recovery_runbook.md](research_recovery_runbook.md)
- [stage2_midday_redesign.md](stage2_midday_redesign.md)
- [stage2_midday_target_redesign.md](stage2_midday_target_redesign.md)
- [stage2_midday_high_conviction.md](stage2_midday_high_conviction.md)
- [stage2_midday_direction_or_no_trade.md](stage2_midday_direction_or_no_trade.md)
- [stage3_midday_policy_paths.md](stage3_midday_policy_paths.md)

Reference docs for architecture and tooling:

- [architecture.md](architecture.md)
- [detailed_design.md](detailed_design.md)
- [gcp_user_guide.md](gcp_user_guide.md)
- [stage12_counterfactual.md](stage12_counterfactual.md)
- [stage12_confidence_execution.md](stage12_confidence_execution.md)
- [stage12_confidence_execution_policy.md](stage12_confidence_execution_policy.md)

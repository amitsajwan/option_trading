# Intraday Profit Execution Plan

This is the active control document for the `ml_pipeline_2` trading-system workstream.

Use it for:

- current product goal
- current verified state
- story tracking
- operator runbook for the active `S3` run
- final go / no-go decision framing

For onboarding and historical context, use [midday_recovery_handover.md](midday_recovery_handover.md).

## Product goal

We are building:

- a profitable BankNifty intraday trading system

The product goal is:

- repeatable positive net returns after cost
- acceptable drawdown and trade count
- a policy simple enough to operate and explain

The product goal is not:

- proving a MIDDAY-only strategy forever
- preserving CE/PE symmetry for its own sake
- maximizing model complexity

`MIDDAY` is the current research wedge, not the business mission.

## Truth table

Current as of `2026-04-11`:

| Item | Verified truth |
| --- | --- |
| Story status | `S0 DONE`, `S1 DONE`, `S2 DONE`, `S3 IN_PROGRESS`, `S4/S5 PENDING` |
| Active GCP run root | `ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/` |
| Baseline run root | `ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline` |
| Current grid behavior | baseline trains Stage 1 fresh; balanced run reuses Stage 1 from baseline |
| Verified grid launch shape | `python3 -m ml_pipeline_2.run_staged_grid --config ... --run-output-root ... --model-group ... --profile-id ...` |
| Deferred live inference gap | `oracle_rolling_*` needs a serving-time lookup/input path before production hand-off |

## Current reality

These points are established from completed work:

1. The current release-path candidate is:
- Stage 1 entry gate
- Stage 2 direction gate
- fixed `L3` / `L6`
- Stage 12 confidence-selection policy

2. The raw oracle is roughly balanced across the key windows.
- validation oracle was about `51% CE`
- holdout oracle was about `48% CE`

3. Stage 2 is the main distortion source.
- validation actionable mix was over-amplified toward CE
- holdout actionable mix was over-amplified toward PE
- this is regime amplification, not robust direction detection

4. Wrapper-level and threshold-level fixes are closed.
- symmetric threshold sweeps
- asymmetric threshold sweeps
- dual-side fractions
- side caps
- manual Stage 2 overrides

5. `S2` is closed with a `NO` verdict.
- `0` cross-window stable directional features at `d >= 0.10`
- current Stage 2 features do not justify retraining as-is

6. `S3` is the active workstream.
- the feature brief is approved
- code changes are implemented
- the live grid run is active on GCP

## Working interpretation

The current Stage 2 setup behaves like a regime amplifier.

That means:

- it can look acceptable inside one regime
- it fails when feature-direction relationships change across windows
- retraining the same setup is not justified

The approved `S3` direction is therefore:

- keep weak, consistent anchors
- remove confirmed regime-followers where they are part of the redesign brief
- add regime-context features such as `oracle_rolling_*`
- test whether the redesign creates cross-window directional stability

## Story tracker

Status values: `TODO` / `IN_PROGRESS` / `DONE` / `STOPPED`

| Story | Title | Status | Owner | Gate |
| --- | --- | --- | --- | --- |
| S0 | Freeze the operating baseline | `DONE` | `CORE` | - |
| S1 | Verify Stage 12 under manual Stage 2 overrides | `DONE` | `CORE` | - |
| S2 | Stage 2 feature signal analysis | `DONE` | `CORE` | Result: `NO` |
| S3 | Feature brief plus one bounded redesign cycle | `IN_PROGRESS` | `CORE + TEAM` | Active GCP run |
| S4 | Final proof assessment | `TODO` | `CORE` | Wait for `S3` |
| S5 | Product decision | `TODO` | `PM + CORE` | Wait for `S4` |

### Story 0

Status: `DONE`

Facts frozen:

- Stage 2 is the bottleneck
- Stage 1 is not the main skew source
- the raw oracle is not the main skew source

### Story 1

Status: `DONE`

Result:

- full Stage 12 override testing did not produce a publishable candidate
- threshold and wrapper tuning are closed as primary solutions

### Story 2

Status: `DONE`

Result:

- `NO` - current Stage 2 features do not contain enough cross-window directional signal to justify retraining as-is

Artifact:

- generated memo lives under the completed `03_stage3_balanced_gate_fixed_guard` run on GCP:
  - `analysis/stage2_feature_signal_diagnostic/stage2_feature_signal_memo.md`

Implication:

- `S3` is a feature-redesign path, not a vanilla retraining path

### Story 3

Status: `IN_PROGRESS`

Approved direction:

- add `compute_rolling_oracle_stats()` in `pipeline.py`
- inject `oracle_rolling_*` into the Stage 2 frame before labeling
- use `fo_midday_direction_regime_v1`
- run a new two-lane grid:
  - `s3_regime_baseline`: trains Stage 1 fresh
  - `s3_regime_balanced`: reuses Stage 1 from baseline

Implementation status:

| Change | Status |
| --- | --- |
| Rolling oracle feature computation | `DONE` |
| Stage 2 frame enrichment | `DONE` |
| `fo_midday_direction_regime_v1` registration | `DONE` |
| `staged_grid.stage3_direction_regime_v1.json` | `DONE` |
| Live GCP grid launch | `RUNNING` |

### Story 4

Status: `TODO`

Goal:

- assess whether the redesigned candidate is publishable

### Story 5

Status: `TODO`

Goal:

- make a business decision with no open-ended research state left

## Active S3 operator runbook

Use this runbook for the active `S3` branch only.

### 1. Check current live run

Active run root:

```text
ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/
```

Check grid and baseline status:

```bash
cat ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/grid_status.json
cat ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline/run_status.json
tail -40 ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline/state.jsonl
```

### 2. If the live run must be restarted

Verified launch command shape:

```bash
python3 -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage3_direction_regime_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

### 3. After `01_s3_regime_baseline` completes

Run the active diagnostics against:

```text
ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline
```

Feature signal diagnostic:

```bash
python3 -m ml_pipeline_2.run_stage2_feature_signal_diagnostic \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline
```

Skew diagnostic:

```bash
python3 -m ml_pipeline_2.run_stage12_skew_diagnostic \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline
```

Confidence execution policy:

```bash
python3 -m ml_pipeline_2.run_stage12_confidence_execution_policy \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_direction_regime_v1/run/runs/01_s3_regime_baseline
```

### 4. What TEAM must deliver to CORE

- the final `grid_status.json`
- baseline run `summary.json`
- Stage 2 feature-signal diagnostic output
- Stage 12 skew diagnostic output
- Stage 12 confidence execution policy output

## S3 success criteria

The redesign is only interesting if it improves directional robustness, not just one-window economics.

Required checks:

- cross-window stable directional features: `>= 3` at `d >= 0.10`
- CE/PE gap vs oracle on holdout: `<= 15pp`
- CE/PE gap vs oracle on validation: `<= 15pp`
- holdout direction agreement vs oracle: `>= 50%`
- holdout CE precision: `>= 40%`
- no reliance on post-hoc side caps or ad hoc threshold rescue

## Deferred scope

Known `S4` dependency:

- `oracle_rolling_*` is a training-time feature family
- live `ml_pure` inference cannot derive it from snapshots alone
- production hand-off needs a serving-time lookup/input path for the current day

This does not block `S3`.
It must be resolved before any production release decision.

## Active ownership

- `CORE`: architecture decisions, feature interpretation, `S4` memo, final keep/reject call
- `TEAM`: run execution, artifact collection, raw output delivery

## Rules for the team

- do not reopen threshold tuning as a primary path
- do not present PE-only subsets as a product result
- do not treat historical docs as active instruction
- use this file and the handover as the only active docs

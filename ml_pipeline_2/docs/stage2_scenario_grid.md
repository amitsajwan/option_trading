# Stage 2 Scenario Grid

## Purpose

This grid is the supported next-step research program when Stage 2 shows weak global validation quality but meaningful regime-local signal. It is designed to answer two questions in one run set:

- which Stage 2 session or feature scenario is actually strongest
- whether the apparent winner is robust under day-level resampling instead of being a one-split artifact

The implementation is reusable for other underlyings and alternative time windows because the grid is built on top of a normal staged base manifest.

## Scenario Axes

The current scenario grid explores three controlled axes around the `best_edge_time_focus` branch:

- session filters
  - `OPENING`
  - `MORNING`
  - `MIDDAY`
  - `LATE_SESSION`
  - `MORNING+MIDDAY`
  - `ALL_DAY`
- label variants
  - current focused abstain rule
  - stricter opposing-return ceiling
- feature pools
  - current winner pool
  - time-aware pool
  - OI/IV pool

This is intentionally not a blind search. Every scenario is interpretable and tied back to a concrete hypothesis.

## Stage 1 Reuse

Most Stage 2 scenario lanes do not change Stage 1 at all. To avoid retraining the same Stage 1 search repeatedly, grid runs can declare:

- `reuse_stage1_from`

This references an earlier successful `run_id` in the same grid and tells the execution layer to:

1. load the prior lane's Stage 1 selection and final packages
2. verify Stage 1 compatibility against the current manifest
3. rescore the current lane's validation and holdout frames with those packages
4. skip fresh Stage 1 training for that lane

This is the main runtime-saving mechanism for session-filter and Stage 2-only feature-pool variants.

## Robustness Probe

The grid supports a `selection.robustness_probe` block. After lane execution completes, the grid:

1. ranks runs by the standard staged criteria
2. selects the top `k` candidates
3. runs day-level bootstrap resampling on their Stage 2 scored validation and holdout splits

The current implementation resamples by `trade_date` and reports:

- bootstrap distribution of ROC-AUC
- bootstrap distribution of Brier
- gate-pass rate under resampling

This is meant to reduce false confidence from one favorable split.

## Artifacts

Each staged run now writes:

- `stages/stage2/diagnostics.json`
- `stages/stage2/diagnostics_scores/research_train.parquet`
- `stages/stage2/diagnostics_scores/research_valid.parquet`
- `stages/stage2/diagnostics_scores/final_holdout.parquet`

The grid summary now includes:

- scenario-level run ranking
- per-run Stage 2 robustness probe output for the top candidates
- the configured probe settings and evaluated run ids
- orchestration integrity and grid status path

The execution layer now also writes authoritative status artifacts:

- `run_status.json` inside each lane root
- `grid_status.json` at the grid root

These are the canonical lifecycle files for operator tooling and release safety.

## How To Reuse For Other Data Or Time Periods

1. Create or copy a normal staged base manifest for the new underlying or time window.
2. Change the base manifest only for data-specific concerns:
   - `inputs`
   - `windows`
   - model search space if needed
3. Point `inputs.base_manifest_path` in the scenario grid manifest to that base manifest.
4. Keep or edit the scenario run list depending on the hypotheses you want to test.
5. Run the grid with `ml_pipeline_2.run_staged_grid`.

This keeps the scenario program separate from the underlying-specific training definition.

## Recommended Operator Flow

1. Run the scenario grid.
2. Inspect `grid_summary.json`.
3. Focus first on:
   - validation ROC-AUC
   - validation Brier
   - robustness probe gate-pass rate
4. Only then take one winner into a full staged run.
5. Keep production blocked until a full staged candidate clears gates.

## Run Safety And Restart Rules

The scenario grid now treats run-root reuse as an explicit operator decision.

- Default mode is `fail_if_exists`.
  - If the grid root already contains artifacts, the command fails immediately.
  - This is the safe default for long-running research jobs.
- `resume` is only for a cleanly completed root.
  - If `grid_summary.json` already exists, the command returns that summary instead of rerunning.
  - If a lane root contains partial artifacts without `summary.json`, resume is rejected.
- `restart` archives the old root before starting again.
  - The previous root is renamed with an `.abandoned_<timestamp>` suffix.
  - Use this when a prior run is known to be contaminated or interrupted.

Locks are also enforced:

- one grid lock per grid root
- one run lock per lane root

This prevents duplicate launches against the same directories and makes operator intent explicit.

Integrity rules:

- lane summaries now carry `execution_integrity`
- grid summaries now carry `orchestration_integrity`
- release is blocked if run integrity is not `clean`

### Recommended Commands

Fresh run:

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_scenarios_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_scenarios_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

Intentional restart after a contaminated run:

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_scenarios_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_scenarios_v1/run \
  --run-reuse-mode restart \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

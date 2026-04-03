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

# ml_pipeline_2 Docs

This directory contains the maintained module documentation for `ml_pipeline_2`.

The current docs set is reference-first. Active execution state should come from:

- the checked-in config being run
- the run root under `ml_pipeline_2/artifacts/...`
- persisted status artifacts such as `run_status.json`, `grid_status.json`, `state.jsonl`, and `summary.json`

There is no single "active control document" in this folder anymore.

## Current Reference Docs

- [architecture.md](architecture.md)
  - package boundary, staged flow, artifact model, and current design rules
- [detailed_design.md](detailed_design.md)
  - file-by-file design map for `src/ml_pipeline_2`
- [execution_architecture.md](execution_architecture.md)
  - execution-state, reuse, and long-running job architecture
- [gcp_user_guide.md](gcp_user_guide.md)
  - operator guide for local/GCP research, grid, campaign, publish, and failure handling

## Current Analysis / Tooling Docs

- [stage12_counterfactual.md](stage12_counterfactual.md)
  - reference note for the Stage 1+2 counterfactual diagnostic
- [stage12_confidence_execution.md](stage12_confidence_execution.md)
  - reference note for confidence execution analysis
- [stage12_confidence_execution_policy.md](stage12_confidence_execution_policy.md)
  - reference note for confidence execution policy analysis

## Current Code Entry Points

Primary CLIs owned by this package:

- `python -m ml_pipeline_2.run_research`
- `python -m ml_pipeline_2.run_staged_release`
- `python -m ml_pipeline_2.run_staged_grid`
- `python -m ml_pipeline_2.run_training_campaign`
- `python -m ml_pipeline_2.run_training_factory`
- `python -m ml_pipeline_2.run_staged_data_preflight`
- `python -m ml_pipeline_2.run_publish_model`

Key checked-in config families:

- `ml_pipeline_2/configs/research/staged_dual_recipe.*.json`
- `ml_pipeline_2/configs/research/staged_grid.*.json`
- `ml_pipeline_2/configs/campaign/*.json`

## Training Research Journal

Dated research session logs live in [`training/`](training/). Each file covers one research iteration (hypothesis → runs → findings). Start here when resuming model work.

- [training/INDEX.md](training/INDEX.md) — quick-reference table of all sessions and key findings
- [training/MODEL_STATE_20260426.md](training/MODEL_STATE_20260426.md) — session: staged pipeline + regime_fix grid (2026-04-26/27)

## Historical Research Notes

2026-09-16: moved out of this directory into
[`../../docs/archive/ml_pipeline_2_bypass_stage2_recovery/`](../../docs/archive/ml_pipeline_2_bypass_stage2_recovery/)
— these were already self-labeled "not the current operating instruction"
here but still cluttered this folder alongside the maintained docs above.
Covers the April 2026 MIDDAY-recovery redesign track and the related
bypass_stage2 investigation (`JIRA_BYPASS_STAGE2_RECIPE_MODEL_ANALYSIS.md`,
`STAGE3_RECIPE_INVESTIGATION.md`, `STAGE3_TRADE_LOSS_ANALYSIS.md`,
`PROPER_TRAINING_STRATEGY_V1.md`, `RECOVERY_BASELINE_20260422.md`), plus the
12 files previously listed here. See that folder's own index for the full
list and how the pieces relate.

## Repo-Level Runbooks

Cross-system runbooks remain outside this module because they cover more than `ml_pipeline_2`:

- [`../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
- [`../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [`../../docs/runbooks/GCP_DEPLOYMENT.md`](../../docs/runbooks/GCP_DEPLOYMENT.md)
- [`../../docs/runbooks/README.md`](../../docs/runbooks/README.md)

Do not add maintained architecture or design docs at the `ml_pipeline_2` module root.

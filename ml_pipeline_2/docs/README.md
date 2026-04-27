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

These files are retained for context and evidence. They are not the current operating instruction:

- [intraday_profit_execution_plan.md](intraday_profit_execution_plan.md)
- [midday_recovery_handover.md](midday_recovery_handover.md)
- [research_recovery_runbook.md](research_recovery_runbook.md)
- [stage2_feature_signal_memo_template.md](stage2_feature_signal_memo_template.md)
- [stage2_midday_redesign.md](stage2_midday_redesign.md)
- [stage2_midday_target_redesign.md](stage2_midday_target_redesign.md)
- [stage2_midday_high_conviction.md](stage2_midday_high_conviction.md)
- [stage2_midday_direction_or_no_trade.md](stage2_midday_direction_or_no_trade.md)
- [stage2_midday_grid.md](stage2_midday_grid.md)
- [stage2_recovery_review.md](stage2_recovery_review.md)
- [stage2_scenario_grid.md](stage2_scenario_grid.md)
- [stage3_midday_policy_paths.md](stage3_midday_policy_paths.md)

## Repo-Level Runbooks

Cross-system runbooks remain outside this module because they cover more than `ml_pipeline_2`:

- [`../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
- [`../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [`../../docs/runbooks/GCP_DEPLOYMENT.md`](../../docs/runbooks/GCP_DEPLOYMENT.md)
- [`../../docs/runbooks/README.md`](../../docs/runbooks/README.md)

Do not add maintained architecture or design docs at the `ml_pipeline_2` module root.

# ml_pipeline_2 Docs

This directory contains the maintained module documentation for `ml_pipeline_2`.

Use this index to choose the right document category first.

## Active

- [intraday_profit_execution_plan.md](intraday_profit_execution_plan.md)
  - use this for current status, active run roots, story tracking, and go/no-go decisions
- [midday_recovery_handover.md](midday_recovery_handover.md)
  - use this first when a new engineer takes over the current staged recovery line

Only these two docs should contain active next-step instructions.

## Reference

- [architecture.md](architecture.md)
  - package boundary, staged flow, artifact model, and design rules
- [detailed_design.md](detailed_design.md)
  - file-by-file design map for `src/ml_pipeline_2`
- [execution_architecture.md](execution_architecture.md)
  - supplemental execution-path design notes
- [gcp_user_guide.md](gcp_user_guide.md)
  - module-level training, release, and publish handoff notes
- [ubuntu_gcp_runbook.md](ubuntu_gcp_runbook.md)
  - Ubuntu/GCP environment notes
- [stage12_counterfactual.md](stage12_counterfactual.md)
  - reference note for the Stage 1+2 counterfactual diagnostic
- [stage12_confidence_execution.md](stage12_confidence_execution.md)
  - reference note for confidence execution analysis
- [stage12_confidence_execution_policy.md](stage12_confidence_execution_policy.md)
  - reference note for confidence execution policy analysis

Supported staged manifest:

- [`../configs/research/staged_dual_recipe.default.json`](../configs/research/staged_dual_recipe.default.json)
- [`../configs/research/staged_grid.prod_v1.json`](../configs/research/staged_grid.prod_v1.json)
  - production-oriented research grid for the staged pipeline
  - base-manifest driven, so the same grid runner can be reused for other instruments by swapping `inputs.base_manifest_path`

Repo-level documents that remain outside this module because they are cross-system:

- [`../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
- [`../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [`../../docs/runbooks/GCP_DEPLOYMENT.md`](../../docs/runbooks/GCP_DEPLOYMENT.md)
- [`../../docs/runbooks/README.md`](../../docs/runbooks/README.md)

## Historical

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

Historical docs are retained for evidence and context.
They are not the current operating instruction.

Do not add maintained architecture or design docs at the `ml_pipeline_2` module root.

# ml_pipeline_2 — bypass_stage2 / MIDDAY recovery archive

Moved here 2026-09-16 from `ml_pipeline_2/docs/` during a documentation
volume cleanup. Every file below already self-declared as historical or was
part of a closed investigation with zero remaining references from current
docs (`ml_pipeline_2/docs/README.md`, `architecture.md`, `detailed_design.md`)
— they were just still sitting in the same directory as the maintained docs,
making the module look like it had far more live documentation than it does.

## MIDDAY staged-model recovery track (2026-04-10 to 2026-04-17)

A closed redesign sequence for a MIDDAY model group. Start with
`research_recovery_runbook.md` for the reconstruction order if this work
ever needs revisiting; it links the rest in sequence.

- `research_recovery_runbook.md` — top-level reconstruction guide
- `midday_recovery_handover.md` — handover context for the track
- `intraday_profit_execution_plan.md` — the former S0-S5 control document
- `stage2_midday_redesign.md`, `stage2_midday_target_redesign.md`,
  `stage2_midday_high_conviction.md`, `stage2_midday_direction_or_no_trade.md`,
  `stage2_recovery_review.md`, `stage2_scenario_grid.md`,
  `stage2_feature_signal_memo_template.md` — sequential Stage 2 redesign
  attempts
- `stage3_midday_policy_paths.md` — the downstream Stage 3 policy-path batch

Still-current siblings of this track (NOT moved — still referenced as live
diagnostic-tool docs from `ml_pipeline_2/docs/README.md`):
`stage12_counterfactual.md`, `stage12_confidence_execution.md`,
`stage12_confidence_execution_policy.md`.

## bypass_stage2 investigation (2026-04-22 to 2026-04-26)

A separate but related closed investigation: a `bypass_stage2` pipeline run
produced 1,432 trades that lost money (PF 0.35). Root-caused to Stage 3
recipe model having zero signal.

- `RECOVERY_BASELINE_20260422.md` — starting point, verified remote run roots
- `JIRA_BYPASS_STAGE2_RECIPE_MODEL_ANALYSIS.md` — the investigation writeup
  (status: Investigation Complete)
- `STAGE3_RECIPE_INVESTIGATION.md` — the investigation plan
- `STAGE3_TRADE_LOSS_ANALYSIS.md` — detailed loss analysis
- `PROPER_TRAINING_STRATEGY_V1.md` — the resulting revised training strategy

Note: `JIRA_STAGED_RECIPE_RISK_BASIS_FIX.md` (a separate, later bug fix) is
**not** in this archive — it's still actively cited by the current
`ml_pipeline_2/docs/training/OPTION_LABEL_CONTRACT.md` for an open audit
item and remains in `ml_pipeline_2/docs/`.

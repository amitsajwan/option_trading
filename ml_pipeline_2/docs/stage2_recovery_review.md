# Stage 2 Recovery Review

## Why This Batch Is Diagnosis-First

The completed grid already tested the nearest label-filter lever. Moving `min_directional_edge_after_cost` from `0.0010` to `0.0014` reduced rows slightly but did not produce a meaningful lift in Stage 2 quality. The best branch, `best_edge_time_focus`, improved drift and ranking modestly, but it still failed both hard gates. That is enough evidence to stop broad search and inspect signal quality directly.

## Why Threshold Tuning Is Not The Lever

- The top Stage 2 runs cluster tightly on Brier around `0.25` even when the edge filter changes.
- `best_edge_time_focus` improved ROC-AUC only to roughly `0.532`, still well below the `0.55` gate.
- This pattern suggests the current Stage 2 target/view combination is underpowered. More threshold nudging is unlikely to recover the missing separation.

## Why Calibration Is Deferred

Stage 2 already trains against a `brier` objective. A post-hoc calibration step can help only after the model shows stronger ranking power. With both ROC-AUC and Brier failing materially, calibration-first work would risk polishing a weak target rather than fixing the underlying label/view quality.

## Evidence The Diagnostics Must Produce

The Stage 2 diagnostics artifact is intended to answer the questions that the grid cannot:

- Is `direction_up_prob` collapsing around `0.5`, or do positive and negative labels separate meaningfully?
- Is the calibration error concentrated in a few deciles or broadly poor?
- Does the label prevalence or edge distribution shift materially across train, validation, and holdout?
- Are the failures concentrated in specific time-of-day buckets or expiry regimes?

If the diagnostics show weak separation and weak regime-local quality at the same time, the next batch should escalate to a larger Stage 2 label/view redesign. If the diagnostics show reasonable ranking with isolated confidence problems, then calibration can be reconsidered later as a secondary step.

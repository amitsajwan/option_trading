# Architecture Review AR2 (Post T07)

Date: `2026-02-21`
Scope checkpoint: after T07 (baseline + walk-forward validation complete)

## Reviewed Areas

1. Model design coherence between T06 and T07
2. Validation integrity and leakage risk
3. Train/eval API boundaries for upcoming threshold optimization

## Findings

1. T07 reuses T06 feature-selection and model-build logic (`train_baseline.py`) so train/eval behavior stays consistent.
2. Fold generation is day-based and chronological, preventing overlap between train/valid/test windows.
3. Current labeled dataset has 5 days, so walk-forward with `3/1/1` yields one fold; this is structurally correct but statistically thin.
4. Leakage risk remains controlled:
   - features are past-only (T04 guard tests),
   - labels are fixed-symbol forward windows (T05 guard tests),
   - folds are strictly time-ordered (T07 tests).

## Decisions

1. Keep single-source training stack for T06/T07 (no duplicate model code).
2. Keep walk-forward report JSON as canonical input for T08 threshold optimization.
3. Require multi-fold walk-forward in future by expanding labeled days before model promotion.

## Refactor Actions

No blocking refactor required now.

Accepted structural changes:

1. Exposed reusable baseline pipeline builder in `train_baseline.py`.
2. Added dedicated `walk_forward.py` module with fold builder + aggregate reporting.

## Risks to Revisit in T08/T09

1. Probability calibration may be unstable with low fold count.
2. Thresholds optimized on one fold may overfit; enforce robustness checks across additional periods.

# Walk-Forward Spec V1 (T07)

> T28 add-on: this evaluator now supports purged/embargoed folds and feature-profile selection for leakage control.

Walk-forward validation evaluates model stability across sequential time windows.

## Fold Construction

Folds are built on trading days (not random rows):

- train block: `train_days`
- purge gap after train: `purge_days` (optional, default `0`)
- validation block: `valid_days`
- embargo gap after validation: `embargo_days` (optional, default `0`)
- test block: `test_days`
- slide window by `step_days`

Default config:

- `train_days=3`
- `valid_days=1`
- `test_days=1`
- `step_days=1`
- `purge_days=0`
- `embargo_days=0`

Each fold preserves strict chronology:

`max(train_days) < min(valid_days) < min(test_days)`

With purge/embargo:

`max(train_days) < min(purge_days) < min(valid_days) < min(embargo_days) < min(test_days)` (when those gaps are non-empty)

## Per-Fold Process

For each side (`CE`, `PE`):

1. Filter to valid labels (`<side>_label_valid == 1`).
2. Train baseline pipeline on train block.
3. Evaluate metrics on validation block.
4. Evaluate metrics on test block.

## Outputs

Report includes:

1. fold-wise metrics and row counts
2. aggregate mean/std metrics across folds
3. feature list and model config used

Primary output:

- `ml_pipeline/artifacts/t07_walk_forward_report.json`

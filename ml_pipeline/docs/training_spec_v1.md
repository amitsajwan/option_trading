# Training Spec V1 (T06)

Baseline training uses separate binary classifiers for CE and PE labels.

## Input

- Labeled dataset from T05 (`t05_labeled_features.parquet`)

## Feature Selection

Use numeric columns only, excluding:

1. Identity columns (`timestamp`, symbols, date metadata)
2. Label and outcome columns (`ce_label`, `pe_label`, returns, MFE/MAE, validity flags, and path-aware TP/SL fields)

Feature profile support:

- `all`
- `futures_options_only` (excludes `spot_*`, `basis*`, and `depth_*` feature families)

## Split Policy

Chronological split per side:

- Train: 70%
- Validation: 15%
- Test: 15%

No random shuffling.

## Model

- `XGBClassifier` (binary logistic)
- Deterministic settings:
  - fixed `random_state`
  - `n_jobs=1`
  - `subsample=1.0`
  - `colsample_bytree=1.0`
- Missing value handling via `SimpleImputer(strategy="median")`

## Metrics

Computed on validation and test splits:

- accuracy
- precision
- recall
- f1
- brier
- roc_auc (if both classes present)
- pr_auc (if both classes present)

## Outputs

- Model package (`joblib`) containing CE and PE pipelines + metadata
- Training report (`json`) including split statistics, metrics, and feature importance

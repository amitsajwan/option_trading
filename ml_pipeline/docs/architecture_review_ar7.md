# Architecture Review AR7 (Post T29)

Date: `2026-02-22`  
Scope checkpoint: after T29 (model zoo + hyperparameter optimization)

## Reviewed Areas

1. Model design and feature-profile boundaries
2. Leakage defenses and CV integrity
3. Tuning governance and model-selection contract

## Findings

1. Futures+options-only modeling boundary is enforced end-to-end (`feature_profile=futures_options_only`), aligned with live input availability.
2. Leakage defense stack is active and evidenced:
   - purged/embargoed walk-forward configured (`train=180d`, `valid=30d`, `test=30d`, `step=30d`, `purge=1d`, `embargo=1d`)
   - synthetic leakage injection detected strongly on both sides in T28 (`auc_lift ~0.46`).
3. Model search is deterministic and reproducible through T29 training-cycle artifacts:
   - `experiments_total=6`
   - best: `fo_no_opening_range__logreg_c1`
   - objective: `rmse=0.4970504264`.
4. Calibration/threshold policy (T31) is explicit and fold-backed:
   - selected calibration: `isotonic` for CE and PE
   - dual thresholds: `CE=0.76`, `PE=0.67`.

## Decisions

1. Keep `futures_options_only` as Phase-3 canonical profile.
2. Keep leakage-audit + purged walk-forward as mandatory promotion gate.
3. Keep training-cycle champion package contract (`feature_columns`, preprocessing metadata, selected model spec) as the only deployable model artifact format.

## Refactor Actions

No blocking refactor required at AR7 gate.

Accepted follow-up improvements:

1. Expand T29 search budget and add explicit stopping governance (time budget + minimum improvement delta).
2. Add fold-level regime tags (trend/volatility buckets) to the leaderboard for robustness visibility.
3. Promote calibration reliability tables (bin counts + expected vs observed) to first-class review artifact.

## Risks for AR8+

1. Search depth remains modest (`6` experiments); model family coverage can still underfit regime diversity.
2. Strong thresholding reduces trade count and can make realized performance highly sample-sensitive.
3. Validation integrity is good, but production-quality outcome now depends on execution/reconciliation stack quality.

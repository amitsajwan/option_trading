# Threshold Optimization Spec V1 (T08)

> T31 add-on: v2 pipeline adds probability calibration comparison (`identity`, `platt`, `isotonic`), reliability bins, purged/embargoed walk-forward support, and dual-mode CE/PE policy evaluation.

Threshold optimization converts model probabilities into trade decisions.

## Objective

Choose CE and PE decision thresholds that maximize expected net return on validation folds.

## Data Flow

1. Build walk-forward folds (`train_days`, `valid_days`, `test_days`, `step_days`).
2. For each fold and side:
   - train model on train block
   - score probabilities on valid and test blocks
3. Sweep threshold grid on validation predictions.
4. Select threshold by validation objective.
5. Evaluate chosen threshold on test predictions.

## Threshold Grid

- `threshold_min`
- `threshold_max`
- `threshold_step`

## Trade Rule

At row `i` for a given side:

- trade if `prob_i >= threshold`
- net return for traded row: `forward_return_i - cost_per_trade`
- no-trade rows contribute `0`

## Optimization Target

Primary metric:

- `mean_net_per_trade` on validation rows

Tie-breakers:

1. higher trade count
2. lower threshold (deterministic ordering)

## Outputs

- selected threshold for CE
- selected threshold for PE
- grid search table
- validation summary
- test evaluation at chosen threshold

Artifact:

- `ml_pipeline/artifacts/t08_threshold_report.json`
- T31 artifact:
  - `ml_pipeline/artifacts/t31_calibration_threshold_report.json`

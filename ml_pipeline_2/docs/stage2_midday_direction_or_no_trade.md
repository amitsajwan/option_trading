# Stage 2 MIDDAY Direction Or No-Trade

## Purpose

This batch replaces the forced binary `CE` vs `PE` Stage 2 problem with a hierarchical decision:

1. predict whether a MIDDAY row is tradeable at all
2. predict CE vs PE only conditionally on the row being tradeable

The goal is to stop forcing the model to assign direction on rows that should really be abstains.

## Training Design

Stage 2 now trains two binary models inside one stage:

- `trade_gate`
- `direction`

The trade gate sees all Stage 2 rows and learns `trade vs no-trade`.
The direction model sees only rows marked actionable by the Stage 2 target-redesign rule and learns `CE vs PE`.

Inference emits:

- `direction_trade_prob`
- `direction_up_prob`
- `ce_prob`
- `pe_prob`

Stage 2 policy then chooses:

- no trade
- CE
- PE

## Batch

The first research batch is:

- `midday_gate_asymmetry_baseline`
- `midday_gate_asymmetry_strict`
- `midday_gate_oi_iv`
- `midday_gate_expiry`
- `midday_gate_selective`

Rules:

- `MIDDAY` only
- Stage 1 reused after the first lane
- Stage 2 search budget widened to support the two-model stage

## Recommended Command

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_midday_direction_or_no_trade_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_midday_direction_or_no_trade_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

# Stage 2 MIDDAY Grid

> Historical research note. Not the current operating instruction. Use `intraday_profit_execution_plan.md` and `midday_recovery_handover.md` for current status and next steps.

## Purpose

This is the post-discovery Stage 2 grid. It exists because the broader Stage 2 scenario program already answered the regime question:

- `OPENING` did not clear the Stage 2 signal check
- `MORNING` did not clear the Stage 2 signal check
- `LATE_SESSION` trained but failed badly on Stage 2 quality
- `MIDDAY` was the only regime that cleared Stage 2 ROC-AUC

The current best baseline from that discovery step is now superseded by `midday_time_aware_pool`. It still fails publication because of Brier, so the next research cycle should optimize only for `MIDDAY` probability quality.

## Baseline

Use the completed `MIDDAY` grid as the evidence anchor:

- validation Stage 2 ROC-AUC: about `0.577`
- validation Stage 2 Brier: about `0.252`
- dominant blocker: `stage2_cv.brier>0.22`

This means the search space is no longer about regime discovery. It is about improving `MIDDAY` Stage 2 separation and confidence quality.

## What This Grid Keeps

- `midday_baseline`
- `midday_strict_winner`
- `midday_time_aware_pool`
- `midday_iv_oi_pool`
- `midday_stricter_abstain`

## What This Grid Drops

- `OPENING`
- `MORNING`
- `LATE_SESSION`
- all-day Stage 2 tuning variants
- mixed `MORNING+MIDDAY` Stage 2 variants

Those branches already consumed compute and did not justify continued search.

## Runtime Controls

This grid is intentionally bounded.

- Stage 1 is reused from `midday_baseline` for all Stage 2-only variants.
- Stage 2 search is budgeted:
  - `max_experiments = 10`
  - `max_elapsed_seconds = 3600`
- robustness probing remains enabled for the top 3 runs.

The goal is to make the next cycle predictable and focused rather than broad.

## Promotion Rule

Promote only if a run achieves:

- Stage 2 validation ROC-AUC above the gate
- Stage 2 validation Brier materially below the current `~0.252` baseline
- acceptable drift
- non-zero robustness gate pass rate

If AUC remains strong but Brier stays around `0.25`, the next move is not more broad grid search. The next move is the focused redesign batch documented in [stage2_midday_redesign.md](/c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_redesign.md).

## Recommended Command

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage2_midday_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage2_midday_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

# BankNifty System Source Of Truth

As-of date: `2026-03-19`

If active docs conflict with code, code wins. If active docs conflict with each other, this file wins.

## 1. Runtime Contract

- Live trading runtime is `strategy_app.main --engine ml_pure`.
- Deterministic runtime is retained for replay and research only.
- Legacy transitional runtime wrapper and registry-backed ML entry overlay have been removed.
- `ml_pipeline_2` is the only supported ML training and publish source for live runtime artifacts.
- Event bus is Redis and persistence is MongoDB.

## 2. ML Artifact Contract

- Runtime artifact selection is by `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP`, or explicit package/report paths.
- In run-id mode, startup is blocked unless publish validation passes and both resolved artifact paths exist.
- `runtime.block_expiry` must stay aligned between training and live runtime policy.
- Active staged label generation is built from forward futures-path barrier labeling in `ml_pipeline_2/labeling/engine.py`, not from deterministic strategy replay exits.

## 3. Runtime Guard Contract

When live ML is enabled:

- rollout stage must be `capped_live`
- `position_size_multiplier <= 0.25`
- guard file approval is mandatory
- guard file must confirm:
  - `approved_for_runtime=true`
  - `offline_strict_positive_passed=true`
  - `paper_days_observed>=10`
  - `shadow_days_observed>=10`

## 4. Current Research + Replay Contract

- Deterministic replay validates B1-B5 behavior and exit attribution.
- After replay validation, decide whether staged views, label recipes, or training windows need regeneration before retraining `ml_pipeline_2`.
- There is no supported runtime ML overlay on top of deterministic votes.

## 5. Canonical References

- Runtime flow: `strategy_app/docs/STRATEGY_ML_FLOW.md`
- Module design: `strategy_app/docs/detailed-design.md`
- Consolidation status: `strategy_app/docs/ENGINE_CONSOLIDATION_PLAN.md`
- Live bring-up: `docs/SUPPORT_BRINGUP_GUIDE.md`
- Current-tree validation: `docs/STRATEGY_SYSTEM_VALIDATION_2026-03-19.md`

## 6. Last Verified Commands

```powershell
python -m snapshot_app.historical.snapshot_batch_runner --validate-only --base .data/ml_pipeline/parquet_data --window-manifest-out .run/window_manifest_latest.json
python -m ml_pipeline_2.run_research --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json --validate-only
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1:historical
python -m strategy_app.main --engine ml_pure --ml-pure-run-id <run_id> --ml-pure-model-group banknifty_futures/h15_tp_auto
```

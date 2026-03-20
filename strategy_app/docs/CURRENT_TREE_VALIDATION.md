# Current Tree Validation

As-of date: `2026-03-20`

This document records the current code-verified state for the active runtime and training contracts.

## Scope

Verified against these code paths:

- `strategy_app/main.py`
- `strategy_app/engines/deterministic_rule_engine.py`
- `strategy_app/engines/pure_ml_engine.py`
- `strategy_app/engines/strategy_router.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/publish.py`
- `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`
- `ops/gcp/publish_runtime_config.sh`

## Confirmed Runtime Lanes

- supported live runtime lane: `strategy_app.main --engine ml_pure`
- replay and research lane: `strategy_app.main --engine deterministic`
- no supported live runtime path layers ML on top of deterministic vote outputs

## Confirmed ML Runtime Contract

- `ml_pure` runtime resolves artifacts by:
  - `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP`
  - or explicit bundle/report paths
- strict run-id switching is enforced by `ml_pipeline_2.publishing.resolver`
- live `ml_pure` requires:
  - `STRATEGY_ROLLOUT_STAGE=capped_live`
  - `STRATEGY_POSITION_SIZE_MULTIPLIER<=0.25`
  - `STRATEGY_ML_RUNTIME_GUARD_FILE`

## Confirmed Deterministic Runtime Facts

- deterministic exits are owner-first, with helper and high-confidence non-owner fallback
- default router facts currently include:
  - `HIGH_VOL` routes `IV_FILTER` and `HIGH_VOL_ORB`
  - `EXPIRY` routes `IV_FILTER` and `VWAP_RECLAIM`
  - `EXPIRY_MAX_PAIN` is not in the default router

## Confirmed Staged Training Facts

- staged `ml_pipeline_2` is the only supported ML training and publish lane on this branch
- staged labels are built from forward futures-path barrier labeling
- staged labels are not built from deterministic strategy exit replay
- staged release computes `publish_assessment`
- staged release publishes only when `publish_assessment.decision=PUBLISH`
- there is no separate champion-selection artifact in the active staged flow

## Relevant Coverage

Relevant regression coverage lives in:

- `strategy_app/tests/test_position_risk.py`
- `strategy_app/tests/test_redis_snapshot_consumer_dedupe.py`
- `strategy_app/tests/test_feature_parity_batch_vs_stream.py`
- `ml_pipeline_2/tests/test_staged_pipeline.py`
- `ml_pipeline_2/tests/test_staged_publish.py`

These tests were not executed as part of this docs-only cleanup pass.

# Strategy System Validation (2026-03-19)

This document validates the March 19 review against the current `strategy_app` and `ml_pipeline_2` codebase.

Rule used for this pass:

- code and executable tests win over narrative docs
- historical research claims stay marked as unverified unless replay artifacts are present

## Scope

- `strategy_app`
- `ml_pipeline_2`
- active docs that currently describe both systems

## Validation Method

Code inspection covered:

- `strategy_app/engines/deterministic_rule_engine.py`
- `strategy_app/engines/strategy_router.py`
- `strategy_app/engines/strategies/all_strategies.py`
- `strategy_app/risk/manager.py`
- `strategy_app/position/tracker.py`
- `strategy_app/runtime/redis_snapshot_consumer.py`
- `strategy_app/main.py`
- `strategy_app/engines/pure_ml_engine.py`
- `strategy_app/engines/rolling_feature_state.py`
- `strategy_app/engines/snapshot_accessor.py`
- `snapshot_app/core/stage_views.py`
- `ml_pipeline_2/src/ml_pipeline_2/labeling/engine.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/publish.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/runtime_contract.py`
- `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`
- `ml_pipeline_2/src/ml_pipeline_2/model_search/walk_forward.py`

Focused tests executed and passing in this pass:

- `strategy_app/tests/test_risk_manager.py`
- `strategy_app/tests/test_redis_snapshot_consumer_dedupe.py`
- `strategy_app/tests/test_position_risk.py`
- `strategy_app/tests/test_feature_parity_batch_vs_stream.py`
- `ml_pipeline_2/tests/test_labeling_engine.py`
- `ml_pipeline_2/tests/test_staged_pipeline.py`
- `ml_pipeline_2/tests/test_staged_publish.py`

## Confirmed Current-State Claims

### 1. Engine lanes

Confirmed:

- The legacy transitional runtime wrapper is removed from the CLI.
- supported runtime lanes are `deterministic` and `ml_pure`.
- `ml_pure` run-id resolution is wired through `ml_pipeline_2` publish metadata and strict artifact validation.

Code:

- `strategy_app/main.py`
- `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/runtime_contract.py`

### 2. Exit ownership

Confirmed:

- deterministic exits are owner-first
- helper exits are explicitly allowed only for configured combinations
- non-owner exits require high confidence
- hard exits still happen before strategy exits

Code:

- `strategy_app/engines/deterministic_rule_engine.py`
- `strategy_app/engines/strategy_router.py`
- `strategy_app/position/tracker.py`

Test coverage:

- `strategy_app/tests/test_position_risk.py`

### 3. Router defaults

Confirmed:

- `EXPIRY_MAX_PAIN` is not in the default router
- `EXPIRY` currently routes `IV_FILTER` and `VWAP_RECLAIM`
- `HIGH_VOL` currently routes `IV_FILTER` and `HIGH_VOL_ORB`

Code:

- `strategy_app/engines/strategy_router.py`

Test coverage:

- `strategy_app/tests/test_position_risk.py`

### 4. Strategy fixes present in code

Confirmed:

- `OI_BUILDUP` exit logic has `min_exit_hold_bars` and `exit_r5m_threshold`
- `EMA_CROSSOVER` exit logic has `ema_exit_min_bars_held` and `ema_exit_min_spread_pct`
- EMA default base confidence is `0.65`

Code:

- `strategy_app/engines/strategies/all_strategies.py`

### 5. Risk/session robustness

Confirmed:

- VIX halt recovery has a missing-data cooldown fallback
- budget/notional lot sizing scales with signal confidence
- session rollover now attempts `on_session_start()` even if `on_session_end()` raises

Code:

- `strategy_app/risk/manager.py`
- `strategy_app/runtime/redis_snapshot_consumer.py`

Test coverage:

- `strategy_app/tests/test_risk_manager.py`
- `strategy_app/tests/test_redis_snapshot_consumer_dedupe.py`

### 6. ML staged pipeline shape

Confirmed:

- staged runtime/publish plumbing is clean
- walk-forward fold builder includes purge and embargo
- runtime `block_expiry` is carried through publish/runtime policy

Code:

- `ml_pipeline_2/src/ml_pipeline_2/staged/publish.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/runtime_contract.py`
- `ml_pipeline_2/src/ml_pipeline_2/model_search/walk_forward.py`

Test coverage:

- `ml_pipeline_2/tests/test_staged_publish.py`

## Rejected Or Stale Claims

### 1. "HIGH_VOL blocks all entries"

Rejected for current code.

Current router behavior:

- `HIGH_VOL` does not hard-block entries
- it routes to `HIGH_VOL_ORB`

Source:

- `strategy_app/engines/strategy_router.py`

### 2. "The immediate config change is removing `EXPIRY_MAX_PAIN` from `Regime.EXPIRY: [EXPIRY_MAX_PAIN, IV_FILTER]`"

Rejected as stale.

Current code is already past that state:

- `EXPIRY_MAX_PAIN` is already absent from the default router
- default `EXPIRY` routing is `[IV_FILTER, VWAP_RECLAIM]`

Source:

- `strategy_app/engines/strategy_router.py`

### 3. "The active staged ML labels were built from broken universal strategy exits"

Rejected for the active `ml_pipeline_2` staged pipeline.

Current staged labeling does not replay deterministic strategy exits. It builds labels from forward futures-path barrier logic over snapshot parquet:

- `label_day_futures()` computes long/short path outcomes from futures OHLC
- `build_labeled_dataset()` applies that day-level labeler
- staged oracle targets are derived from those path labels in `staged/pipeline.py`

Sources:

- `ml_pipeline_2/src/ml_pipeline_2/labeling/engine.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`

Implication:

- deterministic exit-routing fixes do not, by themselves, invalidate staged labels
- retraining is still appropriate when label recipes, stage views, data windows, or runtime assumptions change

### 4. "No check exists for feature drift"

Partially true, overstated as written.

True:

- there is no runtime contract that proves online feature parity against batch stage views

Not true:

- there is already a dedicated parity test for core streamable features

Sources:

- `strategy_app/tests/test_feature_parity_batch_vs_stream.py`
- `strategy_app/engines/rolling_feature_state.py`
- `snapshot_app/core/runtime_features.py`

### 5. "Current review docs are fully consistent with current HEAD"

Rejected.

This pass found active doc drift in:

- `strategy_app/docs/STRATEGY_ML_FLOW.md`
- `strategy_app/docs/TECHNICAL_BRIEFING_CODE_REVIEW_2026-03-19.md`

Main mismatches were:

- HIGH_VOL routing
- staged label provenance
- patch sequencing described as if B1-B5 were still pending

## Historical Research Claims Not Re-verified In This Pass

The following may still be true, but they were not re-proven from source artifacts in this pass:

- `-6.42%` baseline return
- `97.7%` `REGIME_SHIFT` exits
- `EXPIRY_MAX_PAIN` contribution statistics
- per-strategy/per-regime portfolio tables

Reason:

- the referenced `.run/strategy_research/20260228_184650/` outputs are not present in this repository snapshot
- no full replay was run in this pass

Status:

- treat those numbers as historical research notes, not as current-code validation

## Code Changes Applied During Validation

These changes were required to make the current tree match the intended behavior and become testable:

1. Fixed a syntax error in `strategy_app/runtime/redis_snapshot_consumer.py`.
2. Fixed `ExpiryMaxPainStrategy` entry guard inversion in `strategy_app/engines/strategies/all_strategies.py`.
3. Updated `strategy_app/tests/test_risk_manager.py` to match the current confidence-scaled budget sizing behavior.
4. Added regression coverage for:
   - session rollover after `on_session_end()` failure
   - enabled `EXPIRY_MAX_PAIN` entry path

## Recommended Current Narrative

Use this version when describing the system today:

- deterministic is the replay/research lane
- `ml_pure` is the production lane
- owner-first deterministic exits are already implemented
- default router already excludes `EXPIRY_MAX_PAIN`
- staged ML labels are path-based snapshot labels, not deterministic strategy-exit labels
- feature drift remains a real risk, but there is at least partial unit-test coverage for core feature parity

## Related Docs

- `docs/SYSTEM_SOURCE_OF_TRUTH.md`
- `docs/strategy_catalog.md`
- `strategy_app/docs/STRATEGY_ML_FLOW.md`
- `strategy_app/docs/TECHNICAL_BRIEFING_CODE_REVIEW_2026-03-19.md`

# STRATEGY APP
## Technical Briefing & Code Review

Prepared for:
Engineering Team

Review date:
2026-03-19

Scope:
`strategy_app` + `ml_pipeline_2`

## Document Status

This version is aligned to the current repository state. It separates:

- code-verified runtime facts
- residual risks still present in the tree
- historical research claims that were referenced in older review notes but were not revalidated in this pass

Primary cross-check document:
`strategy_app/docs/CURRENT_TREE_VALIDATION.md`

---

## 1. Executive Summary

### Verdict on the external review

The external review had the right architectural instincts, but it overstated several claims as if they were still current runtime facts.

### What the review got right

- The regime layer is still the correct top-level structure for deterministic routing.
- Owner-first exits are important for attribution and now exist in code.
- `ml_pure` is the correct long-term production lane.
- Feature parity between offline views and online reconstruction remains a real risk area.
- `EXPIRY_MAX_PAIN` should remain research-only unless it is reintroduced behind explicit guardrails.

### What is stale or incorrect against current code

- `HIGH_VOL` does not block all entries; current routing is `IV_FILTER + HIGH_VOL_ORB`.
- Default `EXPIRY` routing does not include `EXPIRY_MAX_PAIN`.
- Active staged labels in `ml_pipeline_2` are not built from deterministic strategy exits; they are built from forward futures-path barrier labeling.
- The old transitional ML wrapper lane is already removed.
- B1-B5 are not pending patch proposals anymore; they are already implemented in the current tree.

### Current bottom line

The main gaps today are not the ones framed in the older review. The meaningful remaining issues are:

- historical replay claims need to be re-proven on current code
- full online-vs-batch feature parity is not contract-enforced
- there is still no automated live feedback loop for model staleness

---

## 2. Current Code-Verified System Shape

### 2.1 Runtime lanes

Supported runtime lanes:

- `--engine deterministic`
- `--engine ml_pure`

Removed:

- legacy transitional ML wrapper lane
- legacy runtime ML wrapper that scored deterministic rule outputs

Implication:

- deterministic is the inspectable replay lane
- `ml_pure` is the production lane

### 2.2 Snapshot-to-decision flow

Every snapshot currently flows through:

1. session dedupe and session-boundary handling
2. risk refresh and mark-to-market
3. hard exit checks from `PositionTracker`
4. regime classification
5. deterministic routing or `ml_pure` inference depending on lane
6. risk sizing and signal emission

### 2.3 Regime routing

Current default deterministic router:

| Regime | Default strategies |
|---|---|
| `TRENDING` | `IV_FILTER`, `ORB`, `EMA_CROSSOVER`, `OI_BUILDUP`, `PREV_DAY_LEVEL` |
| `SIDEWAYS` | `IV_FILTER`, `VWAP_RECLAIM`, `OI_BUILDUP` |
| `EXPIRY` | `IV_FILTER`, `VWAP_RECLAIM` |
| `PRE_EXPIRY` | `IV_FILTER`, `ORB`, `OI_BUILDUP` |
| `HIGH_VOL` | `IV_FILTER`, `HIGH_VOL_ORB` |
| `AVOID` | no entries |

This means the common recommendation to "immediately remove `EXPIRY_MAX_PAIN` from the default expiry router" is already satisfied in the current codebase.

### 2.4 Exit ownership

Deterministic exit selection is no longer "first shared exit wins."

Current priority is:

1. hard/system exits from tracker and risk controls
2. owner strategy exit
3. configured helper exit
4. high-confidence non-owner exit

Default helper override:

- `PRE_EXPIRY` + owner `OI_BUILDUP` may use `ORB` as a helper exit

This is the right direction for attribution and replay analysis.

### 2.5 ML pipeline contract

The staged `ml_pure` contract is currently well-defined:

- publish produces a versioned staged bundle
- runtime resolves by `run_id + model_group`
- startup validates the publish decision and resolved artifact paths
- `block_expiry` is carried through publish/runtime policy

The three-stage runtime shape remains correct:

1. entry gate
2. direction choice
3. recipe choice

---

## 3. Current Findings

### 3.1 Findings now resolved in code

The following review items are implemented in the current tree:

| Finding | Current status |
|---|---|
| Strategy-owned exits | implemented |
| Default `EXPIRY_MAX_PAIN` removal | implemented |
| EMA exit hysteresis / quality improvements | implemented |
| OI exit timing guardrails | implemented |
| VIX halt missing-data recovery | implemented |
| Session rollover resilience | implemented |
| Confidence-aware budget lot sizing | implemented |
| Legacy transitional ML wrapper removal | implemented |

### 3.2 Issues discovered during this validation pass

Two real code defects were found and fixed while validating the review:

1. `RedisSnapshotConsumer` session rollover had a broken `try/finally` path that could prevent `on_session_start()` from running after an `on_session_end()` failure.
2. `ExpiryMaxPainStrategy` had an inverted entry guard, which broke its explicit opt-in path even though it is not routed by default.

These were regression-tested after the fixes.

### 3.3 Residual risks that still matter

#### Feature parity is only partially guarded

There is parity test coverage for core streamable features, but there is still no runtime contract that proves the full staged training feature set exactly matches online reconstruction.

#### Historical replay claims are not current-state proof

Older documents cite portfolio metrics such as:

- net return
- drawdown
- exit-reason mix
- per-strategy contribution

Those may be useful research context, but they were not revalidated in this pass because the cited replay artifacts were not present in the repository snapshot.

#### `EXPIRY_MAX_PAIN` still exists in code

It is no longer a default strategy, which is good. But because it still exists as an explicit opt-in strategy, it should be treated as experimental and guarded by research-only documentation until a fresh replay justifies reintroduction.

#### No automatic model-staleness loop

The system still lacks an automated live feedback loop that turns observed live outcomes into retraining signals or stale-model alarms.

---

## 4. ML Integration Assessment

### What is working

- staged publish layout is stable
- runtime contract validation is strict enough to block obviously bad handoffs
- walk-forward validation with purge/embargo is implemented
- live runtime no longer depends on deterministic vote outputs

### What is not yet fully solved

#### Gap 1: full feature parity assurance

Training and runtime still compute equivalent features through different code paths:

- offline staged views/parquet build path
- online rolling/runtime feature path

This is acceptable only if parity testing keeps pace with feature changes.

#### Gap 2: research narratives still drift faster than code

The bigger documentation problem was not just stale wording. It was mixing:

- historical replay conclusions
- current runtime facts
- future implementation plans

That made it too easy to treat old replay findings as if they were still active runtime conditions.

#### Gap 3: live outcome feedback remains manual

Model degradation is still something operators discover through explicit evaluations, not through a built-in monitoring loop.

---

## 5. Historical Claims Kept As Research Context Only

The following claims appeared in older review material but were not reproduced in this pass:

- baseline portfolio return around `-6.42%`
- `REGIME_SHIFT` dominating exits
- `EXPIRY_MAX_PAIN` driving most of the loss
- exact per-regime and per-strategy contribution tables

Current treatment:

- keep them as historical research context only
- do not cite them as current runtime truth until a new replay reproduces them on current code and data

---

## 6. Recommended Next Actions

### 6.1 Replay and refresh the research baseline

Run deterministic replay on current code and current snapshot data, then refresh the portfolio-level tables from reproducible outputs.

This is the most important next validation step because it determines whether the historical research conclusions still hold after the implemented engine changes.

### 6.2 Keep `EXPIRY_MAX_PAIN` experimental

Do not return it to default routing without:

- explicit router override
- replay evidence on current code
- clear entry-frequency and volatility guardrails

### 6.3 Expand feature-parity coverage

Prioritize tests that compare the exact staged features consumed by `ml_pure` against the online feature reconstruction path.

### 6.4 Add a live-model feedback loop

At minimum, add recurring checks that surface:

- decision distribution drift
- outcome drift
- retraining-needed signals

---

## 7. Verification Basis For This Briefing

This briefing was checked against:

- `strategy_app/main.py`
- `strategy_app/engines/deterministic_rule_engine.py`
- `strategy_app/engines/strategy_router.py`
- `strategy_app/engines/strategies/all_strategies.py`
- `strategy_app/risk/manager.py`
- `strategy_app/position/tracker.py`
- `strategy_app/runtime/redis_snapshot_consumer.py`
- `strategy_app/engines/pure_ml_engine.py`
- `ml_pipeline_2/src/ml_pipeline_2/labeling/engine.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/publish.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/runtime_contract.py`
- `ml_pipeline_2/src/ml_pipeline_2/model_search/walk_forward.py`

Focused tests passing in this validation pass:

- `strategy_app/tests/test_risk_manager.py`
- `strategy_app/tests/test_redis_snapshot_consumer_dedupe.py`
- `strategy_app/tests/test_position_risk.py`
- `strategy_app/tests/test_feature_parity_batch_vs_stream.py`
- `strategy_app/tests/test_iv_filter_and_high_vol_router.py`
- `ml_pipeline_2/tests/test_labeling_engine.py`
- `ml_pipeline_2/tests/test_staged_pipeline.py`
- `ml_pipeline_2/tests/test_staged_publish.py`

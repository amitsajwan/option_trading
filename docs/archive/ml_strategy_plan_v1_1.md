# BankNifty Options Algo

## ML Strategy Plan v1.1

March 2026

This document replaces the earlier v1.0 draft with the current, code-verified build plan.

## Current Position

The system infrastructure is usable:

- replay orchestration is working
- deterministic routing is working
- risk and stop handling are working
- evaluation uses capital-weighted metrics
- entry-policy integration exists

The main remaining problem is not plumbing. It is trade selection quality.

The deterministic system has already shown three clear truths:

1. `EXPIRY_MAX_PAIN` was a portfolio killer and should remain unrouted.
2. `OI_BUILDUP` is the strongest current idea, but only in selected contexts.
3. deterministic thresholds are too crude to express the interactions that matter for option buying.

## Root Cause Summary

### 1. Expiry mean reversion was a bad thesis

The earlier portfolio loss was dominated by `EXPIRY_MAX_PAIN`. This was not a stop-loss problem or a router-tuning problem. The strategy thesis itself did not hold up in the tested data.

### 2. Timing and exit arbitration are major non-expiry problems

Average MFE was materially better than realized trade outcomes, and the old system overwhelmingly exited via `REGIME_SHIFT`. That means trade timing, exit ownership, and setup quality were all degrading otherwise tradable moves.

### 3. Rule thresholds do not model interactions well

The current deterministic system still treats features too independently. Intraday option-buy quality depends on combinations:

- momentum plus volume
- regime plus time of day
- option premium plus IV percentile
- OI context plus directional move quality

ML is a better tool for ranking those combinations than hand-tuned threshold trees.

## Architecture Decision

Do not replace the deterministic engine with ML.

Use a hybrid design:

1. deterministic strategies generate candidate trades
2. shared rule policy blocks obviously bad long-premium trades
3. ML ranks or filters the surviving candidates
4. later, ML may assist exits after bar-level logging is upgraded

This keeps the system auditable while letting the model learn feature interactions.

## Model Overview

### Model 1: Regime Classifier

Question:

- what kind of day is this?

Status:

- feasible with current engine hooks

Code hook:

- `RegimeClassifier(model_path=...)` already exists in `strategy_app/engines/regime.py`

Important correction:

- integration is effectively zero-code if the trained model matches the existing 14-feature contract already defined in `RegimeClassifier._extract_model_features()`
- the trained artefact must:
  - accept those 14 features in the same fixed order
  - expose `predict_proba()`
  - emit classes aligned to the `Regime` enum
- the real work is label generation, training, and validation, not engine integration

Primary value:

- cleaner day segmentation
- better routing for `OI_BUILDUP`
- fewer poor sideways or unstable days classified as tradable trend days

### Model 2: Entry Quality Scorer

Question:

- is this a good option buy right now?

Status:

- highest-value model
- clean insertion point already exists

Code hook:

- `EntryPolicy` protocol in `strategy_app/engines/entry_policy.py`

Important correction:

- do not train this model only on historical executed trades
- that creates selection bias
- train it from all candidate entry votes generated in replay, then label those candidates using future path outcomes

Label definition:

- for a `CE` candidate at time `T` with entry premium `P_T`, use future ATM CE premium path
- for a `PE` candidate at time `T` with entry premium `P_T`, use future ATM PE premium path
- default label horizon: 15 bars
- default threshold: 5%

Default binary label:

- `label = 1` if `max(P_{T+1} ... P_{T+15}) / P_T > 1.05`
- `label = 0` otherwise

Equivalent return form:

- `label = 1` if `max((P_{T+i} - P_T) / P_T for i in 1..15) > 0.05`

The `15`-bar horizon and `5%` threshold are tunable hyperparameters and should also be tested at `5/10/15` bars and `3%/5%/8%` thresholds.

Primary value:

- improve trade selection without changing deterministic strategy logic
- reduce marginal entries
- scale or block trades based on learned quality

### Model 3: Exit Timing Model

Question:

- should I keep holding or exit now?

Status:

- not ready for modelling yet

Important correction:

- current logging is insufficient for proper per-bar exit-model training
- `POSITION_MANAGE` logs do not yet persist `mfe_pct` and `mae_pct` per bar
- there is also no dedicated `ExitPolicy` interface equivalent to `EntryPolicy`

Primary value:

- improve exit timing after data contract and logging are upgraded

## Phased Build Plan

### Phase 0: Deterministic Cleanup

Already in progress or complete:

- keep `EXPIRY_MAX_PAIN` out of the default router
- keep hybrid exit ownership priority
- keep entry policy in place
- keep evaluation capital-weighted

Current router status:

- `OI_BUILDUP` remains a core active strategy
- `ORB` remains active as a secondary strategy and helper exit
- `EMA_CROSSOVER` remains routed today, but is a known drag and should be treated as a downgrade candidate, not a trusted core edge
- `VWAP_RECLAIM` remains routed in selected regimes today, but sample size and recent results are weak enough that it should be treated as watch-only / downgrade candidate until expanded-data reruns justify it

Goal:

- establish a stable deterministic baseline before ML

### Phase 1: Data Expansion

Critical path for the whole ML roadmap.

Tasks:

- audit raw archive coverage and data quality
- rebuild snapshot parquet over the full available archive
- verify required `SnapshotAccessor` fields are populated across the rebuilt range
- rerun deterministic baseline over expanded history

Required fields include:

- `ema_9`, `ema_21`, `ema_50`
- `vwap`, `price_vs_vwap`
- `or_width`
- `pcr_change_30m`
- `atm_ce_oi_change_30m`, `atm_pe_oi_change_30m`
- `atm_ce_vol_ratio`, `atm_pe_vol_ratio`
- `iv_skew`

Gate:

- 500+ clean trading days
- stable replay on rebuilt snapshots
- enough candidate entry votes to support Phase 3

### Phase 2: ML Regime Classifier

Tasks:

- generate trailing-outcome regime labels
- train a simple gradient-boosted classifier
- keep rule fallback when model confidence is low
- compare ML routing versus rule routing on holdout data

Gate:

- improvement versus rule-based regime routing on holdout data
- primary gate metric: `OI_BUILDUP` profit factor on holdout days classified as `TRENDING` by the ML regime model must improve versus `OI_BUILDUP` profit factor on holdout days classified as `TRENDING` by the rule regime model
- secondary checks:
  - holdout trade count does not collapse to an unusable sample
  - capital-weighted return for ML-routed `TRENDING` `OI_BUILDUP` trades is not worse than the rule-routed baseline
- not raw classification accuracy alone

### Phase 3: ML Entry Quality Scorer

Tasks:

- build candidate-vote dataset from replay
- one row per directional candidate vote
- label each row using future option-premium path with an explicit causal formula
- train a calibrated entry-quality model
- implement `MLEntryPolicy`

Gate:

- calibrated probabilities on holdout data
- improved capital-weighted results versus rule entry policy

### Phase 4A: Exit Data Contract Upgrade

Tasks:

- extend bar-level position-manage logging
- persist per-bar state needed for exit modelling
- replay full expanded history with enhanced logging

Minimum added fields:

- `mfe_pct`
- `mae_pct`
- `pnl_pct`
- `bars_held`
- `high_water_premium`
- stop state
- target state
- trailing state
- owner strategy and regime context

Gate:

- validated per-bar position dataset across full replay range

### Phase 4B: Exit Timing Model

Tasks:

- define an explicit exit-policy interface
- build bar-level labels
- train and test an exit timing model
- compare to deterministic exit baseline

Gate:

- better holdout performance than deterministic exit logic
- no unacceptable drawdown deterioration

## Candidate Dataset Decision

The first ML-ready dataset should be the candidate-vote dataset for Model 2.

Why:

- it has the cleanest interface
- it does not require live architecture changes
- it directly targets the current weakness: poor setup selection

Dataset rule:

- one row per directional entry vote
- use future ATM option premium path as the label proxy, because that matches current engine execution semantics
- direction-specific path:
  - `CE` vote -> future ATM CE premium path
  - `PE` vote -> future ATM PE premium path
- default primary label:
  - `1` if `max(P_{T+1} ... P_{T+15}) / P_T > 1.05`
  - `0` otherwise
- also persist auxiliary labels for alternate horizons and thresholds so the training pipeline can compare sensitivity instead of hardcoding one choice forever

## Success Metrics

Do not use arbitrary absolute targets like `PF > 1.5` as phase gates.

Use relative, holdout-tested improvement:

- better capital return than deterministic baseline
- better profit factor than deterministic baseline
- lower or equal max drawdown
- calibrated model probabilities
- stable performance by year and regime

## Risks

### 1. Small sample overfitting

Mitigation:

- expand data first
- use simple models first
- use time-series splits only

### 2. Selection bias

Mitigation:

- train Model 2 from all candidate votes, not only taken trades

### 3. Label leakage

Mitigation:

- strict timestamp discipline
- unit tests and spot checks for label generators

### 4. Microstructure drift

Mitigation:

- evaluate by year
- retrain on rolling windows once the system is proven

## Immediate Next Steps

1. expand the snapshot dataset
2. generate the candidate-vote label dataset
3. rerun the deterministic baseline on expanded history
4. only then start training Model 1 and Model 2

## Implementation Note

The current repository already supports:

- optional ML regime inference
- pluggable entry policy
- replay-based historical research

The next implementation milestone is therefore not model training. It is data contract completion.

# Strategy Catalog

This catalog documents the deterministic strategy families currently implemented in `strategy_app` and their active default routing.

Primary code:

- `strategy_app/engines/strategies/all_strategies.py`
- `strategy_app/engines/strategy_router.py`
- `strategy_app/engines/deterministic_rule_engine.py`

## 1. Active Router Map

From `StrategyRouter._entry_sets`:

- `TRENDING`: `IV_FILTER`, `ORB`, `OI_BUILDUP`, `PREV_DAY_LEVEL`
- `SIDEWAYS`: `IV_FILTER`, `VWAP_RECLAIM`, `OI_BUILDUP`
- `EXPIRY`: `IV_FILTER`, `VWAP_RECLAIM`
- `PRE_EXPIRY`: `IV_FILTER`, `ORB`, `OI_BUILDUP`
- `HIGH_VOL`: `IV_FILTER`, `HIGH_VOL_ORB`
- `AVOID`: no entries

Fallback exit candidates come from the shared exit strategy set:

- `ORB`
- `VWAP_RECLAIM`
- `OI_BUILDUP`

Selection is owner-first:

- owner strategy exit wins when present
- configured helper exits are allowed for specific owner/regime pairs
- fallback shared exits are only used when no owner/helper route is available

Helper exit override currently configured:

- `PRE_EXPIRY` + owner `OI_BUILDUP` allows helper exit from `ORB`

## 2. Strategy Families

### `IV_FILTER`

- role: non-directional veto / skip layer
- entry output: `SKIP` + `AVOID` on high IV percentile or `vix_spike_flag`
- key inputs: `iv_regime`, `iv_percentile`, `vix_spike_flag`

### `ORB`

- role: opening-range breakout
- entry: CE on `orh` break, PE on `orl` break
- exit: regime shift when price falls back inside opening-range boundary
- key inputs: `orh`, `orl`, `orh_broken`, `orl_broken`, `vol_ratio`, `pcr`

### `HIGH_VOL_ORB`

- role: ORB profile specialized for `HIGH_VOL`
- entry and exit: same core ORB logic with a stricter high-vol profile

### `OI_BUILDUP`

- role: directional OI continuation
- entry: CE for long buildup, PE for short buildup
- exit: unwind plus adverse 5-minute return

### `EMA_CROSSOVER`

- role: EMA alignment trend follower
- entry: CE when `ema_9 > ema_21 > ema_50`, PE for inverse alignment
- exit: stack violation

### `VWAP_RECLAIM`

- role: VWAP reclaim or rejection strategy
- entry: CE or PE based on side of VWAP with volume and momentum confirmation
- exit: cross back over VWAP against the position direction

### `PREV_DAY_LEVEL`

- role: previous-day high or low breakout continuation
- entry: CE above `prev_day_high`, PE below `prev_day_low`
- exit: prior-day level re-entry with a small buffer and minimum hold bars

## 3. Not Routed By Default

### `TRADER_COMPOSITE`

- role: trader-style composite decision layer
- architecture:
  - day classifier
  - setup scorer
  - option tradability scorer
- supported internal setups:
  - ORB retest continuation
  - VWAP pullback continuation
  - failed breakout reversal
- key inputs:
  - opening range structure
  - VWAP acceptance/rejection
  - futures momentum and volume
  - OI/PCR as confirmation
  - ATM option premium and liquidity
- active usage:
  - routed by experimental profile `det_setup_v1`
  - not part of the default production profile

### `ORB_RETEST_CONTINUATION`

- role: research-only setup primitive
- current status: retained as a standalone experiment, but superseded by `TRADER_COMPOSITE` for `det_setup_v1`

### `VWAP_PULLBACK_CONTINUATION`

- role: research-only setup primitive
- current status: retained as a standalone experiment, but superseded by `TRADER_COMPOSITE` for `det_setup_v1`

### `FAILED_BREAKOUT_REVERSAL`

- role: research-only setup primitive
- current status: retained as a standalone experiment, but superseded by `TRADER_COMPOSITE` for `det_setup_v1`

### `EXPIRY_MAX_PAIN`

- implemented in code
- not part of the default router
- treat as research-only unless explicitly enabled in controlled experiments

## 4. Position And Risk Notes

- final entry action is chosen by `DeterministicRuleEngine` after regime classification, strategy votes, and policy/risk checks
- risk parameters and router overrides can be injected per run context
- exit priority is:
  1. owner exit vote
  2. configured helper exit
  3. fallback shared exits only when owner/helper routes are unavailable
  4. system stops from `PositionTracker` and `RiskManager`

## 5. Related Docs

- [README.md](README.md)
- [CURRENT_TREE_VALIDATION.md](CURRENT_TREE_VALIDATION.md)
- [STRATEGY_ML_FLOW.md](STRATEGY_ML_FLOW.md)
- [ENGINE_CONSOLIDATION_PLAN.md](ENGINE_CONSOLIDATION_PLAN.md)

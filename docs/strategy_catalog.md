# Strategy Catalog

This catalog documents deterministic strategy families currently implemented in `strategy_app` and their active routing.

Primary code:

- `strategy_app/engines/strategies/all_strategies.py`
- `strategy_app/engines/strategy_router.py`
- `strategy_app/engines/deterministic_rule_engine.py`

## 1. Active Router Map

From `StrategyRouter._entry_sets`:

- `TRENDING`: `IV_FILTER`, `ORB`, `EMA_CROSSOVER`, `OI_BUILDUP`, `PREV_DAY_LEVEL`
- `SIDEWAYS`: `IV_FILTER`, `VWAP_RECLAIM`, `OI_BUILDUP`
- `EXPIRY`: `IV_FILTER`, `VWAP_RECLAIM`
- `PRE_EXPIRY`: `IV_FILTER`, `ORB`, `OI_BUILDUP`
- `HIGH_VOL`: `IV_FILTER`, `HIGH_VOL_ORB`
- `AVOID`: no entries

Exit vote candidates come from the shared exit strategy set:

- `ORB`, `EMA_CROSSOVER`, `VWAP_RECLAIM`, `OI_BUILDUP`

Selection is owner-first, not universal-first:

- owner strategy exit wins when present
- configured helper exits are allowed for specific owner/regime pairs
- non-owner exits require high confidence

Helper exit override currently configured:

- `PRE_EXPIRY` + owner `OI_BUILDUP` allows helper exit from `ORB`

## 2. Strategy Families

## `IV_FILTER`

- Role: non-directional veto / skip layer.
- Entry output: `SKIP` + `AVOID` on high IV percentile or `vix_spike_flag`.
- Key inputs: `iv_regime`, `iv_percentile`, `vix_spike_flag`.
- Code: `IVRegimeFilter`.

## `ORB`

- Role: opening range breakout.
- Entry: CE on `orh` break, PE on `orl` break, confidence adjusted by `vol_ratio` and `pcr`.
- Exit: regime shift when price falls back inside opening range boundary.
- Key inputs: `orh`, `orl`, `orh_broken`, `orl_broken`, `vol_ratio`, `pcr`.
- Code: `ORBStrategy`.

## `HIGH_VOL_ORB`

- Role: ORB profile specialized for `HIGH_VOL` regime routing.
- Entry/Exit: same ORB logic with a stricter profile for early high-vol windows.
- Key profile overrides: `vol_ratio_min=1.8`, `max_entry_minute=90`, `confidence_base=0.70`.
- Code: `HighVolORBStrategy` (extends `ORBStrategy`).

## `OI_BUILDUP`

- Role: directional OI continuation.
- Entry: CE for long buildup, PE for short buildup, `SKIP/AVOID` on unwinding.
- Exit: unwind + adverse 5m return (`REGIME_SHIFT`).
- Key inputs: `fut_oi_change_30m`, `fut_oi`, `fut_return_15m`, `fut_return_5m`, `pcr`, `vol_ratio`.
- Code: `OIBuildupStrategy`.

## `EMA_CROSSOVER`

- Role: EMA alignment trend follower.
- Entry: CE when `ema_9 > ema_21 > ema_50` and close confirms, PE for inverse alignment.
- Exit: stack violation.
- Key inputs: `ema_9`, `ema_21`, `ema_50`, `fut_close`, spread thresholds.
- Code: `EMAcrossoverStrategy`.

## `VWAP_RECLAIM`

- Role: VWAP reclaim/rejection strategy.
- Entry: CE/PE based on side of VWAP with momentum and volume confirmation.
- Exit: cross back over VWAP against position direction.
- Key inputs: `vwap`, `fut_close`, `vol_ratio`, short-horizon return.
- Code: `VWAPReclaimStrategy`.

## `PREV_DAY_LEVEL`

- Role: previous-day high/low breakout continuation.
- Entry: CE above `prev_day_high`, PE below `prev_day_low` with volume filter.
- Exit: none explicit in strategy; lifecycle exits handled by system/other exit votes.
- Key inputs: `prev_day_high`, `prev_day_low`, `fut_close`, `vol_ratio`, `pcr`.
- Code: `PrevDayLevelBreakout`.

## 3. Not Routed by Default

## `EXPIRY_MAX_PAIN`

- Implemented in code (`ExpiryMaxPainStrategy`) but not part of router default entry sets.
- Treated as research-only unless explicitly enabled through router overrides in controlled experiments.

## 4. Position and Risk Notes

- Final entry action is chosen by `DeterministicRuleEngine` after:
  - regime classification
  - strategy votes
  - policy/risk checks
- Risk parameters and router/regime overrides can be injected per run context.
- Exit priority:
  - owner exit vote
  - configured helper exit
  - high-confidence non-owner exit
  - system stops from `PositionTracker` / `RiskManager`

## 5. Related Docs

- [strategy_eval_architecture.md](strategy_eval_architecture.md)
- [OPEN_SEARCH_REBASELINE_RUNBOOK.md](OPEN_SEARCH_REBASELINE_RUNBOOK.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)

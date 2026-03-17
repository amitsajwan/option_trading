# Strategy Research Findings (2026-02-28)

## Scope

- Dataset analyzed: current historical snapshot parquet only
- Coverage available in snapshot parquet: 210 trading days
- Date range analyzed: 2020-01-01 to 2024-02-09
- Minute snapshots processed: 77,876
- Engine analyzed: current `DeterministicRuleEngine`
- Capital model used for portfolio results: current lot sizing and `RISK_CAPITAL_ALLOCATED=500000`
- Risk scenarios tested:
  - `baseline_default`
  - `tight_stop_10`
  - `medium_stop_20`
  - `tight_stop_10_trailing`
  - `medium_stop_20_trailing`

Detailed CSV outputs and the generated report are in:

- `.run/strategy_research/20260228_184650/`

## Headline Result

Current default strategy stack is not profitable on the available snapshot dataset.

- Baseline net capital return: `-6.42%`
- Baseline max drawdown: `-7.50%`
- Baseline win rate: `45.4%`
- Baseline profit factor: `0.69`

This is not a stop-loss tuning problem first. It is primarily an entry/routing problem.

## What Is Working

### 1. `OI_BUILDUP` is the only clearly positive contributor in the current live mix

Baseline contribution by entry strategy:

- `OI_BUILDUP`: `+0.43%` capital contribution, `PF 1.28`
- `ORB`: `-0.08%` capital contribution, roughly flat
- `VWAP_RECLAIM`: `-0.02%` capital contribution, sample too small
- `EMA_CROSSOVER`: `-0.38%` capital contribution
- `EXPIRY_MAX_PAIN`: `-6.56%` capital contribution

Interpretation:

- `OI_BUILDUP` has the best current edge.
- `ORB` may still be usable, but not as a major source of returns in the current router.
- `VWAP_RECLAIM` does not have enough sample size to justify confidence.

### 2. Pre-expiry routing is the best regime slice

Baseline contribution by regime:

- `PRE_EXPIRY`: `+0.43%`
- `SIDEWAYS`: `-0.26%`
- `TRENDING`: `-0.22%`
- `EXPIRY`: `-6.56%`

Interpretation:

- `PRE_EXPIRY` is the only regime bucket with meaningful positive contribution.
- `EXPIRY` is overwhelming the rest of the portfolio and erasing the useful edge elsewhere.

## What Is Not Working

### 1. `EXPIRY_MAX_PAIN` is the main reason the stack loses money

Baseline:

- Trades: `330 / 698` total trades
- Win rate: `44.8%`
- Avg trade option return: `-2.98%`
- Profit factor: `0.46`
- Total capital contribution: `-6.56%`

This single module explains almost the entire portfolio loss.

Ad hoc rerun with `EXPIRY_MAX_PAIN` removed from the router:

- Net capital return improved from `-6.42%` to `-0.09%`
- Max drawdown improved from `-7.50%` to `-1.71%`

That is strong enough to justify disabling it immediately.

### 2. `EMA_CROSSOVER` is negative and not earning its place

- Trades: `46`
- Win rate: `43.5%`
- Profit factor: `0.76`
- Total capital contribution: `-0.38%`

This is not catastrophic like `EXPIRY_MAX_PAIN`, but it is still a net drag.

### 3. Tight stops make the portfolio worse, not better

Scenario comparison:

- `baseline_default`: `-6.42%`
- `medium_stop_20`: `-13.65%`
- `medium_stop_20_trailing`: `-14.65%`
- `tight_stop_10`: `-28.26%`
- `tight_stop_10_trailing`: `-31.05%`

Why this happens in the current system:

- lot sizing scales inversely with stop distance
- tighter stop means more lots
- option premiums are noisy intraday
- you get stopped more often and at larger position size

So a 10% stop is not “safer” here. In this engine it increases leverage and makes losses worse.

## Core Mistakes

### 1. We are trying to fix a weak entry stack with stop tuning

The data says the opposite approach is needed.

- Only `0.86%` of baseline trades exited via `STOP_LOSS`
- `97.7%` of baseline trades exited via `REGIME_SHIFT`
- Only `6` trades hit target

That means stop/target settings are barely driving outcomes in the current stack. Most trades are being closed by strategy logic before risk logic matters.

### 2. The router is over-allocating to expiry-day mean reversion

The expiry regime produces the largest trade count and the largest loss bucket.

- Thursdays are the worst weekday by far: `-5.71%` capital contribution
- January and February are the worst months in the sample

That is consistent with the expiry-heavy routing and the `EXPIRY_MAX_PAIN` module dominating the portfolio.

### 3. We are using universal exit strategies across all open positions

Current engine behavior:

- one position at a time
- entries come from the selected strategy
- exits are evaluated by a universal exit set

That means a trade entered by one strategy can be closed by another strategy's regime-shift logic. This blurs ownership and makes stop tuning less relevant.

### 4. The dashboard evaluation metric is misleading

The dashboard service compounds raw option trade return percentages as if each trade used the full portfolio. That is not how the engine sizes risk.

This research used capital-weighted returns based on:

- actual `lots`
- option premium
- `BANKNIFTY_LOT_SIZE`
- `capital_allocated`

That is the correct way to judge whether the portfolio is making or losing money.

### 5. We do not yet have enough snapshot coverage to lock conclusions for production

Raw layer-1 market data covers much more history than the snapshot parquet currently evaluated. The strategy analysis today is over `210` snapshot days, not the full raw archive.

That is enough to find clear failure modes, but not enough to finalize parameter tuning.

## Strong Conclusions

### Disable now

1. Disable `EXPIRY_MAX_PAIN` as an entry strategy immediately.
2. Do not deploy `10%` or `20%` replay stop configs as default portfolio settings.

### Downgrade / retest

1. Move `EMA_CROSSOVER` out of direct entry responsibility unless a retest proves otherwise.
2. Keep `VWAP_RECLAIM` on watch only until more sample accumulates.

### Keep as candidates

1. `OI_BUILDUP` should remain a core candidate.
2. `ORB` is worth keeping, but it needs a cleaner routing and exit context.

## Recommended Refactor Path

### 1. Separate entry ownership from exit ownership

Change the engine so that:

- strategy-specific exits are evaluated first for the strategy that opened the trade
- universal exits are reserved for:
  - hard stop
  - trailing stop
  - time stop
  - global risk halt

This will make performance attribution cleaner and make stop behavior actually measurable.

### 2. Remove `EXPIRY_MAX_PAIN` from the default router

Suggested immediate router change:

- `Regime.EXPIRY`: use `IV_FILTER` and possibly `VWAP_RECLAIM` only
- do not route new entries to `EXPIRY_MAX_PAIN`

### 3. Make capital-weighted metrics the default evaluation view

The evaluation summary should show:

- end capital
- net capital return
- capital drawdown
- per-strategy capital contribution

Raw option return can still be shown, but it should not be the primary KPI.

### 4. Run future research on the expanded snapshot dataset

Before parameter tuning, rebuild snapshot parquet so the analysis covers the full historical archive available in the raw inputs.

## Best Immediate Experiment Set

1. Rerun the default portfolio with `EXPIRY_MAX_PAIN` removed.
2. Rerun with `EMA_CROSSOVER` also removed.
3. Keep baseline stop sizing while retesting entry logic. Do not tighten stops yet.
4. Run a cleaner `OI_BUILDUP + ORB` portfolio with no expiry-day entries and strategy-owned exits.
5. Compare all results on capital-weighted metrics only.

## Files To Review

- Scenario summary: `.run/strategy_research/20260228_184650/scenario_summary.csv`
- Baseline by strategy: `.run/strategy_research/20260228_184650/baseline_by_strategy.csv`
- Baseline by regime: `.run/strategy_research/20260228_184650/baseline_by_regime.csv`
- Baseline by year: `.run/strategy_research/20260228_184650/baseline_by_year.csv`
- Baseline trades: `.run/strategy_research/20260228_184650/trades_baseline_default.csv`

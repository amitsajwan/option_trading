# Rules v1 — proposed for first backtest

**Date:** 2026-05-20
**Author:** Claude (acting as trader-proxy until human trader engages)
**Status:** ready to run; awaiting review

## Approach

Four rules, two windows, mechanical exits. Same audit gates as the ML
search. No optimization — these are baseline rules a discretionary
intraday BankNifty trader would naturally articulate. If any survive
both windows, we have something. If none do, we have evidence that the
problem isn't model architecture — the simple human-readable signals
don't carry edge either.

## The four rules

### R1 — Opening Range Break Down → Buy ATM PE

Bearish breakout of the first 15-minute range. Trades the immediate
follow-through.

**Entry (all must hold):**
- Opening range formed (`ctx_opening_range_ready == 1`)
- Breakout down (`ctx_opening_range_breakout_down == 1`)
- PCR rising from open (`vel_pcr_delta_open > 0`) — bearish positioning
- Volume accelerating (`fut_flow_volume_accel_1m > 0`) — momentum confirmation
- Above-median ATR (`osc_atr_percentile > 0.5`) — not chop regime

**Disqualifiers:**
- Before 9:30 (no entries while OR forming)
- After 14:30 (no fresh trades late in session)
- Expiry day (option pricing distorted by gamma)

**Exit:** 30% stop, 60% target, 20-min time stop, EOD 15:20 force close.

### R2 — Opening Range Break Up → Buy ATM CE

Mirror of R1. Same logic, opposite direction. PCR falling from open is
the bullish equivalent of R1's rising PCR.

### R3 — Overbought Reversal → Buy ATM PE

Exhaustion at extremes. Market well above VWAP, RSI overbought, EMA9
has rolled over (first sign of distribution), and CE writers are piling
in (supply at the top).

**Entry:**
- RSI overbought (`osc_rsi_14 > 75`)
- ≥ 100 pts above VWAP (`vwap_distance > 100`)
- EMA9 slope negative (`ema_9_slope < 0`) — momentum has rolled
- CE OI building (`vel_ce_oi_delta_30m > 0`) — writers active

**Disqualifiers:** Before 10:00 (need the morning trend to establish),
after 14:30, expiry day.

**Exit:** 25% stop, 40% target, 15-min time stop, EOD 15:20.

### R4 — Trend Continuation → Buy ATM CE

Strong uptrend day. 3-EMA stack, above VWAP, OI building (longs
committing). Trade with the trend, not against it.

**Entry:**
- EMA9 > EMA21 > EMA50 (stack, cross-column comparison)
- EMA9 slope positive
- 5-min return positive (immediate momentum confirming)
- Above VWAP
- Futures OI building (`fut_flow_oi_change_5m > 0`)

**Disqualifiers:** Before 10:00, after 14:30, expiry day.

**Exit:** 25% stop, 50% target, 25-min time stop, EOD 15:20.

## Audit gates (same as ML search)

A rule passes only if **both** windows clear **all four** gates:

1. ≥ 30 trades in window
2. t-stat > 2.0 (positive and significant)
3. 95% bootstrap CI lower bound > 0
4. Net P&L without top-5 days ≥ 0 (outlier survival)

## Stage 1 / "100-pt move filter" — not in v1

The trader's question — "can we use Stage 1's good ROC as an entry
gate?" — is a real opportunity, but Stage 1 was a *direction*
predictor that overfit window-by-window. We can't use it for
direction. But it may have learned *volatility* (when something is
about to happen, regardless of which way).

**Plan:**
1. **Run v1 above as baseline.** No volatility filter.
2. **If any rule shows partial promise** (e.g. clear edge one window,
   neutral the other), add a **cheap volatility proxy** as a shared
   disqualifier:
   - `osc_atr_percentile > 0.5` AND `fut_flow_volume_accel_1m > 0`
   - This is already in R1/R2 — extend to R3/R4 for v2.
3. **If the proxy filter clearly helps**, then justify the engineering
   to train a real Stage 1 abs-move classifier and inject its score as
   a per-minute `stage1_move_score` feature.

Run order: baseline → proxy filter → real Stage 1. Each step gated on
the previous showing signal.

## How to run

```bash
# On the ML VM (where flat v3 + options data live):
cd /opt/option_trading
git pull --ff-only

python -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/run_$(date +%Y%m%d)

# Then read:
cat ml_pipeline_2/artifacts/rules_runs/run_*/leaderboard.md
```

Expected runtime: a few minutes (4 rules × 2 windows = 8 cells; each
cell loads ~60 days of data once, walks minute-by-minute).

## What to look for in the leaderboard

- **No PASS in either window** → discretionary rules also fail.
  Strong evidence the data doesn't carry exploitable edge at this
  horizon. Time to consider Phase 2 pivots (different horizon /
  different data source).
- **PASS in one window, FAIL the other** → same overfit pattern as
  the models. Revisit conditions and try the volatility filter.
- **PASS in both windows** → first non-overfit signal we've seen.
  Inspect trade-by-trade, then move to paper deploy with proper
  guardrails.

## Files

- This doc: `docs/RULES_V1_PROPOSED.md`
- Rule JSONs: `ml_pipeline_2/configs/rules/r{1,2,3,4}_*.json`
- Matrix: `ml_pipeline_2/scripts/rules_pipeline/rule_matrix.json`
- Pipeline entry: `ml_pipeline_2/scripts/rules_pipeline/pipeline.py`

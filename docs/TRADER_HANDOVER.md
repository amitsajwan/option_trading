# Handover to discretionary traders — option-PnL edge search

**Date:** 2026-05-19
**Audience:** Experienced BankNifty intraday option traders
**Ask:** Look at the data we have, write down the rules you'd actually
use to enter, and we'll backtest those rules on the same dataset our
models trained on. If your rules show edge, we re-build the model
around the structure you found. If they don't, we have an honest
answer that the data itself doesn't carry exploitable edge at the
horizon we've been targeting.

---

## 1. The one-line problem

For four years (2020-08 to 2024-10, BankNifty, 1-minute bars), we have
spot, futures, full option-chain (OHLC + Volume + OI per strike) and
~110 derived features per minute. We've trained ~30+ XGBoost variants
to predict "buy ATM PE / CE now → does the option go up ≥ X% in the
next N minutes net of cost?" — and we cannot find a single
configuration that produces statistically-significant edge that
survives on a held-out time window.

**Yet a trader watching velocity / momentum / option-flow on a chart
believes they can pick spots.** That belief is the hypothesis we want
to test directly, without an ML model in the way.

---

## 2. The data — what we have, exactly

**Time range:** 2020-08-03 → 2024-10-31 (1053 trading days, no gaps).
**Granularity:** 1 minute. ~375 bars per trading day (9:15 to 15:30).
**Instrument:** BankNifty options + futures + spot.
**Storage:** Parquet on the ML VM under
`/opt/option_trading/.data/ml_pipeline/parquet_data/`.

### What you actually see per minute (~110 columns)

**Spot / futures price**
- `px_fut_open/high/low/close`, `px_spot_open/high/low/close`
- `ret_1m`, `ret_3m`, `ret_5m` (futures return over N minutes)

**Trend**
- `ema_9`, `ema_21`, `ema_50`, `ema_9_21_spread`
- `ema_9_slope`, `ema_21_slope`, `ema_50_slope`

**Volatility / momentum**
- `osc_rsi_14`, `osc_atr_14`, `osc_atr_ratio`, `osc_atr_percentile`
- `vwap_fut`, `vwap_distance` (distance of price from VWAP)
- `dist_from_day_high`, `dist_from_day_low`

**Futures flow**
- `fut_flow_volume`, `fut_flow_oi`
- `fut_flow_rel_volume_20` (vs 20-bar baseline)
- `fut_flow_volume_accel_1m`
- `fut_flow_oi_change_1m`, `fut_flow_oi_change_5m`
- `fut_flow_oi_zscore_20`

**ATM option flow**
- `opt_flow_atm_strike` (which strike is ATM right now)
- `opt_flow_ce_oi_total`, `opt_flow_pe_oi_total` (total OI on the chain)
- `opt_flow_ce_volume_total`, `opt_flow_pe_volume_total`
- `opt_flow_pcr_oi` (put-call ratio of OI), `pcr_change_5m`, `pcr_change_15m`
- `opt_flow_atm_call_return_1m`, `opt_flow_atm_put_return_1m`
- `opt_flow_atm_oi_change_1m`, `atm_oi_ratio`, `near_atm_oi_ratio`

**Velocity features (delta vs earlier in the same session)**
- `vel_ce_oi_delta_open`, `vel_pe_oi_delta_open` (CE/PE OI build since 9:15)
- `vel_ce_oi_delta_30m`, `vel_pe_oi_delta_30m`
- `vel_ce_oi_build_rate`, `vel_pe_oi_build_rate` (per-minute build)
- `vel_oi_ratio_delta_open`, `vel_oi_ratio_delta_30m`
- `vel_price_delta_open/30m/60m`, `vel_price_acceleration`
- `vel_pcr_delta_open/30m`, `vel_pcr_acceleration`, `vel_pcr_trend_direction`
- `vel_ce_vol_delta_30m`, `vel_pe_vol_delta_30m`
- `vel_options_vol_acceleration`

**Microstructure v3 (added 2026-05-19, 11 features)**
- OI structure: `oi_atm_pe_ce_ratio`, `oi_concentration_5strikes`,
  `max_oi_strike_dist_atm`, `oi_skew_4strikes`, `oi_atm_pe_minus_ce_5m`
- Volume structure: `vol_atm_pe_ce_ratio`, `vol_otm_vs_atm`, `vol_weighted_strike_dist`
- Premium structure: `ce_pe_premium_ratio_atm`, `premium_range_atm_5m`, `wing_premium_ratio`

**Context / regime**
- `time_minute_of_day`, `time_minute_index`
- `ctx_opening_range_ready`, `ctx_opening_range_breakout_up/down`
- `ctx_dte_days`, `ctx_is_expiry_day`, `ctx_is_near_expiry`
- `ctx_is_high_vix_day`
- `ctx_regime_atr_high/low`, `ctx_regime_trend_up/down`, `ctx_regime_expiry_near`

We also have the **full option chain** (every strike, OHLC + Volume +
OI, per minute) underlying the aggregates above.

---

## 3. What we tried — three phases of model search

### Phase A — Direction labels (futures up/down)
- Predict "futures up X% in next N bars."
- 5 separate feature/labelling configurations (C1, F1, B1, exit sweep, G4).
- **All overfit one window, failed the other.** Same recipe that
  produced +200%+ on Aug-Oct lost -800%+ on May-Jul.

### Phase B — Option-PnL labels (per-recipe ATM PE/CE)
- Label: "buy ATM_PE_15 (15-bar hold, stop X%, target Y%) → does it
  net-profit?" Same for ATM_PE_9, ATM_CE_9, ATM_CE_15.
- Trained per-recipe binary XGBoost classifiers, swept entry threshold.
- Multi-bundle config (PE+CE, threshold 0.50, 5-bar RISK_BREACH cooldown)
  produced 64-day replay: 2348 trades, +7.61% net, 51.1% win rate.
- **Audit verdict (the part that killed it):** t=0.48, p=0.63, 95% CI
  contains zero. Net is driven by 5 outlier days out of 64. Trades on
  the top-margin minutes (where the model is most confident) are
  ANTI-predictive (average −4.24%). **Not deployable to real capital.**

### Phase B' — Microstructure features (today, 2026-05-19)
- Added 11 per-strike OI/Volume/Premium structure features (the list
  in section 2 above).
- Backfilled 1053 days.
- Re-ran the full model selection (24 cells = 4 recipes × 3 thresholds
  × 2 windows).
- **1 PASS, 18 FAIL, 5 ERROR.** The single PASS (ATM_PE_15 @ thr=0.55
  Aug-Oct) failed cross-window: same recipe on May-Jul gave t=−1.06
  on a larger sample. Same overfit signature as Phase A.

**Net conclusion across 7 confirmations:** at this prediction horizon
(per-minute entries, ATM-anchored, threshold-gated binary), no model
configuration we've found produces edge that survives both time
windows.

---

## 4. What "edge" means in our audit (so your rule is judged fairly)

Same gates as the model audit. A rule passes only if **all four**
clear in **both** May-Jul AND Aug-Oct 2024 hold-out windows:

1. **Minimum trades:** ≥ 30 in each window (avoid coin-flip results).
2. **t-statistic:** mean P&L / std-error ≥ 2.0 (positive and large).
3. **95% bootstrap CI excludes zero:** lower bound > 0.
4. **Outlier survival:** remove the top-5 P&L trades. The remaining
   trades' net must still be ≥ 0. If your edge collapses without the
   5 biggest wins, it isn't edge — it's having gotten lucky 5 times.

Win rate alone is **not** sufficient. A rule that fires 3 times and
wins all 3 has WR=100% but n=3 — that's noise, not signal.

---

## 5. What we want from the trader

Write down **2 to 4 entry rules** you actually use, in this format:

> **Rule:** Buy ATM PE when *[condition A]* AND *[condition B]*
> AND optionally *[condition C]* — entry price = ATM PE close at that
> minute. Exit: *[stop loss X% / target Y% / time stop N minutes / EOD
> force-close — pick whatever rule you actually use]*.
>
> **Avoid trading when:** *[any disqualifier — first 5 min, expiry day,
> high VIX, etc.]*

Each condition should reference one of the **column names** in section 2,
or describe a simple derivation we can compute (e.g., "VWAP was below
spot for the last 10 bars" → we can derive). Be specific with numbers:
"velocity below -X" needs a value for X.

You can also write rules in plain English and we'll translate — but
the plainer and more specific, the less room for us to mis-implement.

**Don't worry about uniqueness or sophistication.** A boring rule
("buy PE when RSI > 70 in expiry week AND PCR is rising AND opening
range broken down") is exactly what we want to test.

---

## 6. How we'll backtest your rules

For each rule you give us:
1. Compute the entry signal on every minute, every day, 2020-08 → 2024-10.
2. Simulate single-position execution (only one open trade at a time —
   subsequent fires are ignored until the position closes).
3. Apply your exit rule, deduct cost (2 bps round-trip default — tell
   us if you want a different cost model).
4. Run the same statistical audit (section 4) on May-Jul and Aug-Oct 2024.
5. Report: trade count, win rate, t-stat, 95% CI, top-5 outlier share,
   net-without-top-5, P&L by month.

Turnaround: same day per rule.

---

## 7. Open questions you can help us decide

1. **Horizon.** Are we wrong to predict per-minute? Would you rather
   predict "should I trade THIS opening range" (one decision per day)
   or "should I trade in the next 1 hour" instead of every minute?

2. **Anchor.** We always trade ATM. Would you anchor elsewhere — first
   OTM, max-OI strike, max-volume strike?

3. **Exit logic.** Our exits are mechanical (stop X, target Y, time N).
   Do you actually exit on a discretionary signal we should encode
   (e.g., "exit on VWAP reclaim", "exit on opposite OI build")?

4. **Disqualifiers.** What times-of-day or market conditions would you
   refuse to trade in, even if all your entry conditions fired?

---

## 8. Files / references (for the team handing this over)

- This document: `docs/TRADER_HANDOVER.md`
- Feature contract: `snapshot_app/core/snapshot_ml_flat_contract.py`
- Velocity feature definitions: `snapshot_app/core/velocity_features.py`
- v3 microstructure feature builder: `ml_pipeline_2/scripts/feature_builder/build_microstructure_v3.py`
- Model selection pipeline: `ml_pipeline_2/scripts/model_selection/pipeline.py`
- Audit harness (the gates in section 4): `ml_pipeline_2/scripts/model_selection/audit_run.py`
- Today's run output: `/opt/option_trading/ml_pipeline_2/artifacts/model_selection_runs/run_20260519/` (leaderboard.md)
- Prior 64-day audit verdict (the rejected multi-bundle config):
  see memory `project_edge_audit_finding.md`

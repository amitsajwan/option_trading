# Project status & handover — 2026-05-20

This document captures the full state of the BankNifty option-trading
edge-search project after ~4 months of iteration. It's written for an
operator/engineer/trader picking up the work — give it a 20-minute read
and you'll know what we've built, what we've ruled out, what's promising,
and where the next decision sits.

---

## 1. One-line problem statement

We have four years of BankNifty 1-minute data and a full audit
infrastructure. **Can we find any strategy with statistically
significant, cross-regime, deployable edge?**

After ~9 independent test runs (ML and rules), the answer so far is
"only one strategy shows real edge — and only in calm-market regimes."

---

## 2. Data — do we have what we need?

### What we have

**Time range:** 2020-08-03 → 2024-10-31 (1053 trading days, no gaps).
**Instrument:** BankNifty weekly options + futures + spot.
**Granularity:** 1 minute (~375 bars/day, 9:15 to 15:30 IST).
**Storage:** `/opt/option_trading/.data/ml_pipeline/parquet_data/`
on the ML VM.

Three parquet datasets, all 1053 days:
- `snapshots_ml_flat_v3/` — ~110 derived features per minute
- `snapshots_ml_flat_v2/` — older feature set (no per-strike microstructure)
- `options/` — full option chain per minute (CE+PE, all strikes, OHLC + Volume + OI + expiry)

### What we have, by feature category

Documented in full in `docs/TRADER_HANDOVER.md` §2. Summary by group:

- **Price**: futures + spot OHLC, ret_1m/3m/5m
- **Trend**: EMA 9/21/50 + spreads + slopes
- **Volatility/momentum**: RSI-14, ATR-14, ATR ratio/percentile, VWAP
  + distance, distance from day H/L
- **Futures flow**: volume, OI, vol-accel, OI-change-1m/5m, OI zscore
- **ATM option flow**: ATM strike, total CE/PE OI + Volume, PCR-OI + 5m/15m
  changes, ATM CE/PE return 1m, ATM OI change/ratio
- **Velocity features (sparse — anchored on session times):**
  vel_ce/pe_oi_delta_open/30m, vel_oi_ratio_delta, vel_pcr_*,
  vel_price_delta_open/30m/60m, vel_options_vol_acceleration.
  **Critical finding:** these fire on only ~0.3% of rows (anchor minutes
  only). Cannot be used in per-minute entry conditions — they
  silently NaN-out the rule.
- **Microstructure v3 (added 2026-05-19, 11 features):**
  oi_atm_pe_ce_ratio, oi_concentration_5strikes, oi_skew_4strikes,
  oi_atm_pe_minus_ce_5m, max_oi_strike_dist_atm, vol_atm_pe_ce_ratio,
  vol_otm_vs_atm, vol_weighted_strike_dist, ce_pe_premium_ratio_atm,
  premium_range_atm_5m, wing_premium_ratio.
- **Regime/context**: opening range flags, DTE, expiry-day flag,
  high-VIX-day flag, ATR-regime flags (high/low), trend-regime
  flags (up/down).

### What we do NOT have (and which would matter)

1. **Multi-day rolling features.** All current features are computed
   from intraday data on a single day's snapshot. No "trailing-N-day
   realized vol", "spot vs 20-day MA", "VIX absolute level", etc.
   **This is the biggest gap exposed by our R1S finding** (see §4).
2. **Multi-expiry option chain.** `options/` currently contains only the
   nearest expiry per day. Multi-expiry features (calendar-spread
   ratios, term-structure indicators) were dropped from v3 because the
   raw data doesn't support them.
3. **Live Indian VIX series.** We have a `ctx_is_high_vix_day` flag
   but it's a categorical derived from somewhere — not the underlying
   VIX series, and the flag does NOT track macro-vol periods correctly
   (see §5).

---

## 3. Approach — what counts as "edge"

Same canonical audit gates applied to ML models and rules. A strategy
passes ONLY if **all four** clear in **both** May-Jul AND Aug-Oct 2024
hold-out windows (or per-quarter when running historical sweeps):

1. **Sample size:** ≥ 30 trades in the window
2. **t-statistic:** mean P&L / std-error ≥ 2.0
3. **95% bootstrap CI:** lower bound > 0
4. **Outlier survival:** Net P&L *without* the top-5 days ≥ 0
   (if your edge collapses without the 5 biggest wins, it wasn't edge)

Cost model: 2 bps round-trip baked into every per-trade pnl. Single
position constraint enforced (no overlapping positions).

Audit code: `ml_pipeline_2/scripts/model_selection/audit_run.py`.
This is the project's source of truth on what counts as "real edge."

---

## 4. What we've tried and ruled out

### Phase A (Jan-Apr 2026) — Direction labels via ML
- 5 configurations (C1, F1, B1, exit sweep, G4 family) predicting
  "futures up/down in next N bars" with XGBoost.
- **Verdict:** all overfit one window, failed the other. Same recipe
  produced +200%+ one window, -800%+ the other.

### Phase B (Apr-May 2026) — Option-PnL labels via ML
- Per-recipe binary classifiers (ATM_PE_9, ATM_PE_15, ATM_CE_9,
  ATM_CE_15) predicting "this entry will net positive after costs."
- Multi-bundle deployment (PE+CE @ thr=0.50 + cooldown) produced +7.6%
  net on 64-day replay BUT audit showed t=0.48, p=0.63, CI contains
  zero, top-margin trades anti-predictive. **Not real edge.**

### Phase B' (May 2026) — Microstructure v3 features
- Added 11 per-strike OI/Volume/Premium features. Re-ran 24-cell
  model search.
- **Verdict:** 1 PASS, 18 FAIL, 5 ERROR. The single PASS failed G3
  cross-window. No deployable winner.

### Phase C (May 2026) — Rules v1 + v2 (long-side discretionary)
- Wrote 4 trader-style rules (ORB break, overbought reversal, trend
  continuation) directly translatable to chart rules.
- **Verdict (v1):** 0 PASS / 8 FAIL. Critical sub-finding: most rules
  fired too few times due to (a) my wrong unit assumptions
  (`vwap_distance` is fractional, not points) and (b) including sparse
  velocity features that NaN-out 99.7% of rows.
- **Verdict (v2):** 0 PASS / 10 FAIL after fixing units.
- **Sub-finding:** Both LONG and FADE (same conditions, opposite
  direction) of every setup LOSE. Win rates 40-50%, net-without-top-5
  −200% to −325%. **Structural finding: buying ATM weekly options at
  per-minute granularity bleeds theta regardless of signal.**

### Phase D (May 2026) — Rules v3 (short-side)
- 4 sell-side rules mirroring the long-side conditions (R1S-R4S).
- Added single-leg short support to `execution_sim` (sign-correct
  P&L, stop_pct=100 = 2x credit hard stop).
- **Verdict (single-window):** 1 PASS — R1S on May-Jul 2024 cleared
  every gate (n=282, t=+3.53, ci=[+0.73%,+2.62%], net-w/o-top5 = +183%).
- **Symmetry check:** all 4 short rules had POSITIVE t-stats in at
  least one window where their long counterparts had NEGATIVE t-stats.
  Theta-bleed thesis VALIDATED.
- **Sub-finding (R1S Aug-Oct):** failed (t=-0.54, net-w/o-top5 -205%).
  R1S works in one regime, not another.

### Phase D' (today) — R1S 17-quarter historical sweep
- Ran R1S across every quarter 2020-08 → 2024-10.
- **Verdict:** 6 PASS / 11 FAIL.
- **PASS quarters:** 2020-Aug-Dec, 2021-Q1, 2021-Q4, 2023-Q3, 2024-Q1,
  2024-Q2.
- **FAIL quarters:** all of 2022 (Ukraine/Fed/recession), 2023-Q1/Q2
  (banking stress), 2024-Q3/Oct (Aug 5 carry-unwind + geopolitics),
  plus three marginal fails.
- **The pattern is regime-based, not stochastic.** All PASS are calm
  bullish-drift periods. All FAIL- are macro-vol events.

### Phase D'' (today) — vol-filter attempt
- Added `ctx_is_high_vix_day == 1` as disqualifier (R1SF). Same 17
  quarters re-run.
- **Verdict:** WORSE. 4 PASS / 13 FAIL.
- **Diagnostic:** the flag fires inappropriately on calm-period days
  (killed all 313 trades of 2021-Q1) and DOESN'T fire on the macro-vol
  days we wanted blocked (2023-Q1 banking stress, 2024-Oct geopolitics).
- **Per-quarter feature diagnostic:** NONE of the vol-related per-bar
  features (osc_atr_*, ctx_is_high_vix_day, ctx_regime_atr_*) cleanly
  separate the PASS quarters from FAIL- quarters. In fact, contrast
  often goes the WRONG way (PASS quarters have HIGHER atr_percentile,
  HIGHER ctx_is_high_vix_day rate).

---

## 5. The current best finding: R1S, regime-conditional

**Rule:** `R1S_SHORT_CE_ORBDOWN`
- Direction: SELL ATM CE (collect theta on a likely-overpriced call)
- Setup: Opening range break DOWN + 5-min return negative + spot below VWAP
- Disqualifiers: before 9:30, after 14:30, expiry day
- Exit: 50% target (buy back at half credit), 100% stop (premium doubles),
  20-min time-stop, 15:20 EOD force-close

**Edge profile (across all PASS quarters combined):**
- Win rate ~57-60%
- Net-without-top-5 consistently +50% to +200% per quarter (real edge,
  not outlier-driven)
- Theta capture: most days small positive grind; some days bigger wins;
  occasional losers when momentum continues

**Why it works (theory):** On a calm-bullish-drift day, an ORB-break-down
is usually a counter-trend false move that mean-reverts intraday. The CE
you sell at the apparent breakdown drains theta as spot recovers. Pure
sellers-win-vs-buyers structural advantage on these days.

**Why it fails (regime change):** When macro vol picks up, ORB-break-down
moves actually CONTINUE downward, AND IV expansion means the CE you sold
can spike before mean-reverting. Stop_pct=100 (premium doubles) hits
often. The constant theta-positive grind is wiped out by the catastrophic
stop days.

---

## 6. The open problem

**We need a regime detector that distinguishes "calm bullish drift" from
"macro vol / sideways vol" — and none of our current per-minute features
do it.**

Per-minute features are intraday measurements. The regime is a multi-day
/ weekly broader-market state. Bridge: build daily regime features
from spot price history. Candidates:

- Trailing 20-day realized volatility (std of daily returns)
- Spot distance from 20-day SMA
- 20-day SMA slope (positive/negative)
- VIX absolute level (would require ingesting India VIX series)
- 60-day equity return (proxy for "trend year")

None of these exist in the flat v3 schema today. Building them is a
~1 day engineering task (compute from daily-aggregated spot prices,
merge back into each minute's row).

---

## 7. Way forward — three real options

### Option 1 — Build the regime-detection layer (1-2 weeks)

Add daily-rolling regime features to the flat dataset:
- `regime_rv20` (trailing-20-day realized vol from daily futures returns)
- `regime_dist_sma20` (spot - SMA20) / SMA20
- `regime_sma20_slope` (5-day slope of SMA20)
- `regime_60d_return`

Re-run the 17-quarter R1S sweep with `regime_rv20 < X AND
regime_sma20_slope > 0` as the disqualifier. If this filter cleanly
separates PASS from FAIL- quarters, we have the project's first
deployable edge.

**Risk:** even with the right regime feature, deployment-grade strategy
needs months more of paper-trading to validate the live-vs-backtest gap.
But the BACKTEST edge would be real.

### Option 2 — Accept R1S as regime-conditional, deploy with manual gating

Don't try to encode the regime in code. Instead:
- Identify a small set of human-readable indicators (e.g., "India VIX
  < 15 AND BankNifty above its 20-day MA").
- An operator manually flips R1S on/off based on the regime each week.
- This is exactly what discretionary traders do: have a strategy, only
  run it when conditions match.

**Risk:** human-in-the-loop is not really "automated edge." But it's
honest about what we know.

### Option 3 — Stop searching for alpha, repurpose the infrastructure

The platform built during this project is the real durable asset:
- Strict audit gates that prevent self-deception
- JSONL-canonical / Mongo-derived storage with reconciliation tooling
- Decisions.jsonl per-snapshot trace + observability endpoints
- Rule-based backtest engine (supports long & short, mechanical exits,
  audit integration)
- Walk-forward F1 training + multi-window robustness gate (G3)
- 1053 days of clean BankNifty data

These are valuable independent of finding alpha. Options:
- Paper-trade a marginal known-public strategy (e.g., 0DTE iron condors
  during specific time windows) with proper risk controls
- Open-source the audit harness as a tool for other practitioners
- Use as a learning platform; document the negative findings as a
  resource ("here's what doesn't work in BankNifty intraday")

---

## 8. The trader handover that's still open

`docs/TRADER_HANDOVER.md` was authored 2026-05-19 to invite a real
discretionary trader to articulate their rules. **It has not yet been
sent.** If you want a fresh second opinion before pursuing Option 1
above, send the trader handover doc + this status doc to a senior
intraday BankNifty trader and ask: "given what we've found, would you
believe R1S has edge? What would you add to make it deployable?"

---

## 9. Files / where to start

### Code
- `ml_pipeline_2/scripts/rules_pipeline/` — rule schema, condition
  evaluator, signal generator, data loader, execution sim, single-rule
  CLI, matrix orchestrator, diagnostic scripts.
- `ml_pipeline_2/scripts/model_selection/audit_run.py` — canonical audit
  gates (any new strategy must pass this).
- `ml_pipeline_2/scripts/feature_builder/build_microstructure_v3.py`
  — v3 feature builder (read for reference if extending).
- `snapshot_app/core/snapshot_ml_flat_contract.py` — feature contract.

### Configs
- `ml_pipeline_2/configs/rules/r{1,2,3,4}*.json` — long-side rules (all
  losers, kept for documentation).
- `ml_pipeline_2/configs/rules/r{1,2,3,4}s*.json` — short-side rules.
- `ml_pipeline_2/configs/rules/r1sf_*.json` — R1S + (wrong) high-vix filter.
- `ml_pipeline_2/scripts/rules_pipeline/rule_matrix*.json` — matrices.

### Results (on ML VM)
- `ml_pipeline_2/artifacts/rules_runs/run_20260520/leaderboard.md`
  — main rules baseline (9 rules × 2 windows = 18 cells).
- `ml_pipeline_2/artifacts/rules_runs/r1s_history_*/leaderboard.md`
  — R1S 17-quarter historical sweep (THE key finding).
- `ml_pipeline_2/artifacts/rules_runs/r1sf_history_*/leaderboard.md`
  — failed filter attempt.
- `ml_pipeline_2/artifacts/model_selection_runs/run_20260519/`
  — ML model selection results.

### Docs
- `docs/RUNTIME_DECISION_FLOW.md` — engine gate chain
- `docs/MODEL_OUTPUT_CONTRACT.md` — model output schema
- `docs/OBSERVABILITY_GUIDE.md` — operator cheat-sheet
- `docs/TRADER_HANDOVER.md` — open ask to a discretionary trader
- `docs/RULES_V1_PROPOSED.md` — the rule v1 proposal that became the
  starting point for everything since
- `docs/PROJECT_STATUS_2026-05-20.md` — **this document**

### Memory (Claude's auto-memory for the project)
- `MEMORY.md` (index) and `project_*.md` files in
  `~/.claude/projects/.../memory/`. Read these in order:
  1. `project_storage_contract.md`
  2. `project_replay_cleanup_protocol.md`
  3. `project_overfit_2024_finding.md`
  4. `project_multi_bundle_state.md`
  5. `project_edge_audit_finding.md`
  6. `project_v3_microstructure_verdict.md`
  7. `project_rules_verdict.md`
  8. `project_r1s_regime_finding.md`

---

## TL;DR for someone with 60 seconds

- Data is clean, comprehensive, 1053 days.
- 9 independent test runs (ML + rules); 8 found no edge.
- One strategy works **in calm regimes only**: short ATM CE on opening-range
  break down + bearish momentum. Real edge there (6 of 17 quarters,
  +50–200% net excluding outliers per quarter).
- Regime detector is missing — none of our per-minute features separate
  the calm-regime quarters from the macro-vol-regime quarters.
- Three real paths: build daily regime features and try again,
  accept manual regime gating, or repurpose the infrastructure.

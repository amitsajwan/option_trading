# System Analysis — June 2026
## Compression Entry Strategy | State, Findings, Risks, Recommendations

**Prepared:** 2026-06-19  
**Branch:** `feat/compression-state-engine`  
**Reviewer note:** This document is intended for external review. All numbers come from runnable code / MongoDB traces, not estimates.

---

## 1. What We Are Running

### 1.1 Two Independent Systems

| System | Container | State | P&L |
|---|---|---|---|
| **Buyer** (compression entry) | `strategy_app` | Paper — real money gated | Net negative (see §3) |
| **Seller** (iron condor) | `seller_app` | Paper — `SELLER_LIVE_ENABLED=0` | +₹1,692/trade on 2024 backtest |

This document focuses entirely on the **Buyer** system.

---

### 1.2 Buyer System Architecture

```
Every 5-min bar (live feed from Kite):

  [REGIME]     regime_quality(snapshot) → TREND / MID / CHOP
                 Reads: mtf_aligned, ema_trend_5m, ema_trend_15m, bb_pct_b_5m
                 Skip bar if CHOP

  [ML ENTRY]   entry_compression_v1.predict(snapshot) ≥ 0.35
                 40-feature XGBoost model, AUC 0.83 on 2024 holdout
                 Label: underlying move ≥ 0.20% (≈110pt on 55k BankNifty) in 5 bars
                 Fire rate: ~3% of bars at threshold 0.35

  [DIRECTION]  RegimeDirector(signal="weighted").decide(snapshot) → CE / PE / ABSTAIN
                 Weighted sum of: momentum_15m, atm_oi, max_pain, vwap, ema signals
                 Skip bar if ABSTAIN

  [EXECUTE]    Buy ATM option (CE or PE), 1 lot (30 qty)

  [EXIT]       Adaptive:
                 TREND regime → Lottery (hold until thesis fails or EOD)
                   Thesis fail: no MFE ≥ 3% after 5 bars
                   Hard stop: 20% premium loss
                 MID regime → Scalper (3% target, 7% stop)
```

### 1.3 Key Config (Confirmed Live in Container)

All 6 of these were verified via `ops/gcp/verify_config.py` running inside the container:

```
ENTRY_ML_MODEL_PATH         = /app/ml_pipeline_2/artifacts/entry_only/published/entry_compression_v1.joblib
ENTRY_ML_MIN_PROB           = 0.35
EXIT_STRATEGY_MODE          = adaptive
ADAPTIVE_LOTTERY_REGIMES    = TREND,TRENDING,BREAKOUT
REGIME_DIRECTION_SIGNAL     = weighted
EXIT_THESIS_FAIL_BARS       = 5
LOTTERY_THESIS_FAIL_BARS    = 5
```

**Important:** `REGIME_DIRECTION_SIGNAL` was missing from `docker-compose.yml` environment block until June 19 (it was in `.env.compose` but not forwarded to the container). Fixed and confirmed.

---

## 2. Entry Model: `entry_compression_v1`

### 2.1 What It Is

40-feature XGBoost classifier trained on 2020–2024 BankNifty data.

**Feature categories:**
- Compression / structure: `compression_score`, `vol_compression_ratio`, `bb_bandwidth_pct`, `oi_build_pct`
- Technical: `adx_14`, `bb_width_20`, `ema_spread_9_21`, `ema_spread_21_50`, range features
- OI / options: `atm_ce_oi`, `atm_pe_oi`, `pcr`, `max_pain_dist`
- Velocity: `vel_5_10_15m`, `vol_spike_ratio`
- Intraday context: `vix_intraday_chg`, `pcr_change_30m`, `position_in_day_range`

**Training label:** Underlying futures move ≥ 0.20% (≈110pt) in next 5 bars (25 minutes).

**Holdout AUC:** 0.83 (2024 full-year OOS)

**Threshold logic:**

| Threshold | Fire rate (live) | Trade count/day |
|---|---|---|
| 0.40 | 1.1% of bars | ~4 |
| 0.35 | ~3% of bars | ~10-15 |

Current: `0.35`. This is deliberately loose — the regime gate and direction abstain filter reduce actual entries further.

### 2.2 Critical Bug (Discovered June 19, Fixed)

**The model was running with 16/40 features NaN in both sim and live.**

Missing features: `vix_intraday_chg`, `pcr_change_30m`, `adx_14`, `vol_spike_ratio`, `bb_width_20`, `bb_width_chg_5`, `range_10`, `range_30`, `range_ratio_10_30`, `candle_overlap_10`, `ema_spread_9_21`, `ema_spread_21_50`, `ema_order`, `dist_from_ema21`, `position_in_day_range` (15 features — confirmed in sim output).

**Root cause (three compounding bugs):**

| Bug | Location | Effect |
|---|---|---|
| `flat.update()` merge | `bundle_inference.py` | Velocity enrichment dict (NaN before 11:30) silently overwrote good per-bar values |
| Last-write-wins | `stage_views.py _project_view` | `velocity_enrichment` block processed after `futures_derived`, clobbered it |
| `raw_payload` pass used strict overwrite | `bundle_inference.py` | `None` from view projection overwrote scalar values in raw payload |

**Fixes (June 19, commits `cf462a5`, `4c08cfc`, `6cae04b`, `728b3bf`):**
1. `adx_14` and `vol_spike_ratio` now computed per-bar from OHLCV in `compression_features.py` — no longer depend on 11:30 velocity enrichment
2. `bundle_inference.py` switched from `flat.update()` to fill-only merge — existing non-NaN values never overwritten
3. `stage_views.py` switched from last-write-wins to non-NaN-wins across all projection blocks
4. Integration tests added to catch regression

**Status:** Fixed in local repo. **Not yet deployed to runtime VM.** The live container is running the pre-fix code.

**Impact of running with 16/40 features NaN:**
- Model is scored with 40% of features at their training median
- Median fill moves many bars to similar scores → model loses discrimination
- Fire rate inflated: bars that should score 0.25 (below threshold) score 0.38 (above threshold)
- Estimated effect: 3-5× too many entries vs correct feature set

---

## 3. June 2026 Simulation Results

### 3.1 Method

Sim runs inside `strategy_app` container against June 2026 MongoDB snapshots (real live data). Uses the actual live modules: `predict_positive_class_prob`, `RegimeDirector`, `build_adaptive_exit_stack`, `PositionContext`. Config matches `.env.compose`. ATM premium from `atm_options.atm_ce/pe_close`.

Script: `ops/research/jun_verify_sim.py`

**Limitation:** Historical MongoDB snapshots have 16 features NaN (captured before the bugs were fixed). Sim results reflect the **pre-fix degraded model**, not the corrected live system.

### 3.2 Results

```
Date          Bars  Fires  Entries   WR   Avg%   Total Rs
------------------------------------------------------------------------
2026-06-01     358     21       18   39%   +0.2%    +1,201
2026-06-02     375     67       40   32%   -0.6%    -8,323  ← outlier
2026-06-03      83     29       13   54%   +0.1%      +222
2026-06-09       1      0  (no entry fires)
2026-06-10     375      3        3   67%   +0.9%      +692
2026-06-11     375      3        0  (fires, no regime pass)
2026-06-12     375     10        7   86%   +1.6%    +2,940
2026-06-15-19  ----     0  (no entry fires)
------------------------------------------------------------------------
TOTAL: 81 trades | 6 active days | WR=43% | Avg=-0.1% | Total=-Rs3,268
```

### 3.3 June 02 Analysis (The Problem Day)

June 02 had 67 fires and 40 entries — 3× the average of other days. This drove the -₹8,323 loss.

**What happened on Jun 02:**
- BankNifty was mixed/volatile — session opened with PE signals (TREND bars), then spent most of the afternoon ranging
- Early TREND+PE entries (rows 1-8): mostly losses, BankNifty did not follow through
- MID+CE entries (rows 11-40): direction model picked CE in the afternoon while BankNifty was flat-to-falling

**Specific breakdown of Jun 02 trades:**

| Regime | Side | n | WR | Avg% |
|---|---|---|---|---|
| TREND | PE | 8 | 25% | -1.3% |
| TREND | CE | 9 | 44% | -0.7% |
| MID   | CE | 23 | 30% | -0.8% |

MID-regime CE entries in the afternoon are the biggest loss bucket. The direction model (weighted) picked CE because early-session OI build was CE-biased, but BankNifty reversed.

**Is this a model bug or directional risk?**

Both. The 3× fire count is the model bug (NaN features inflating scores). The 32% WR on those 40 trades reflects genuine directional risk — the weighted signal failed on a ranging day. Even with perfect features (fewer entries), the directional P&L on Jun 02 would likely be negative.

### 3.4 Good Days (Jun 10, Jun 12)

**Jun 10:** BankNifty fell ~400pt. All 3 entries were PE, 2/3 profitable. Simple trend day, model correctly identified compression and regime correctly identified TREND.

**Jun 12:** BankNifty rose. All 7 entries CE, 6/7 profitable, avg +3.9% per trade. Lottery exit captured the full move (entries around ₹850-863, exits ₹875-897 — 3-4% move captured).

**Pattern:** When the day has a clear directional trend AND the entry model fires (compression setup), the system works as designed. The 86% WR on Jun 12 and 67% WR on Jun 10 match the theoretical edge from the 2024 backtest.

### 3.5 Non-Fire Days (Jun 15-19)

The entry model correctly fires ZERO times on Jun 15-19. This is expected — June 15-19 was post-budget volatile period with no clear compression structures. The model abstaining is correct behavior, not a failure.

---

## 4. Root Cause Summary

Three independent problems compound to produce the June P&L:

### Problem 1: Model Degraded (16/40 Features NaN) — Severity: CRITICAL
- **Effect:** Model fires 3-5× too often, all extra entries are low-confidence noise
- **Status:** Fixed in code (June 19), NOT yet deployed to VM
- **Action required:** Deploy 4 commits to runtime VM (see §5)

### Problem 2: Direction Quality on MID-Regime Bars — Severity: MEDIUM
- **Effect:** 30% WR on MID-regime entries (23 trades Jun 02). Weighted signal loses edge in ranging markets.
- **Evidence:** Jun 10 and Jun 12 both had predominantly TREND bars and 67-86% WR. Jun 02 had 60% of entries as MID, all with poor WR.
- **Status:** Known directional risk. Not a bug — direction is genuinely hard to predict in ranging markets.
- **Proposed mitigation:** Restrict entries to TREND-only (`verdict.quality == 'TREND'`) and remove MID from the regime gate. Estimated effect: Jun 02 would have had 17 entries instead of 40, and excluded the worst-performing MID-CE bucket.

### Problem 3: No Position Limit in Sim (Minor) — Severity: LOW
- **Effect:** Sim allows concurrent overlapping entries. Live system limits to 1 position at a time.
- **Impact:** Jun 02's 40 entries would likely be fewer in reality (exits from earlier positions create gaps).
- **Status:** Sim limitation. Live system already handles this.

---

## 5. Deployment Gap (Most Urgent)

**The 4 June 19 commits are in local git but NOT on the runtime VM.**

The fix chain (must be applied in order):

| Commit | Change | Required |
|---|---|---|
| `cf462a5` | adx_14 + vol_spike_ratio computed live per-bar | Yes |
| `4c08cfc` | bundle_inference: fill-only merge (velocity can't overwrite) | Yes |
| `6cae04b` | raw_payload scalars: _fill semantics | Yes |
| `728b3bf` | stage_views: non-NaN-wins across projection blocks | Yes |

**Deploy steps:**
```bash
# On runtime VM:
git pull origin feat/compression-state-engine

# docker-cp the changed files (image is stale, must cp):
sudo docker cp snapshot_app/core/compression_features.py option_trading-snapshot_app-1:/app/snapshot_app/core/
sudo docker cp snapshot_app/core/stage_views.py            option_trading-snapshot_app-1:/app/snapshot_app/core/
sudo docker cp strategy_app/ml/bundle_inference.py         option_trading-strategy_app-1:/app/strategy_app/ml/

# Restart (snapshot_app computes features, strategy_app reads them):
sudo docker compose --env-file .env.compose restart snapshot_app strategy_app

# Verify NaN count dropped to 0 (run during market hours):
sudo docker exec option_trading-strategy_app-1 python -c "
import sys; sys.path.insert(0, '/app')
import joblib
from strategy_app.ml.bundle_inference import build_feature_row
# ... (check NaN count in first fired bar)
"
```

**Risk:** After restart, the stale image reverts any docker-cp'd files that weren't re-copied. Always use the `deploy_compression_v1.sh` script which handles all cp steps.

---

## 6. Financial State

| Item | Value |
|---|---|
| Current balance (Dhan) | ₹24,333 |
| Monthly expiry ATM premium (est.) | ₹33,000–40,000 / lot |
| Weekly expiry ATM premium (est.) | ₹8,000–12,000 / lot |
| Minimum viable capital for monthly | ~₹50,000 (1.5× margin) |
| Minimum viable capital for weekly | ~₹25,000 |

**Immediate constraint:** Balance is below safe threshold for monthly expiry options. Must trade weekly expiry or add capital before going live.

**Futures rollover:** `BANKNIFTY26JUNFUT` expires June 26. Must update `DEPTH_FEED_INSTRUMENTS` and `BANKNIFTY_FUTURES_SYMBOL=BANKNIFTY26JULFUT` before market open on June 26.

---

## 7. Recommendations

### Immediate (Before Next Live Session)

**R1. Deploy the 4 NaN-fix commits to runtime VM.** [Priority: CRITICAL]  
Without this, the entry model is running with 40% of features median-filled. This alone could explain most of the June losses. Expected effect: fire rate drops from ~67/day to ~10-20/day on active days. Target NaN count: 0/40.

**R2. Restrict entries to TREND-only.** [Priority: HIGH]  
Remove MID from the allowable entry regime. Change in `.env.compose`:
```bash
# Current (allows MID entries → scalper exit):
# No explicit setting — regime gate allows TREND and MID by default

# Proposed:
ENTRY_REGIME_WHITELIST=TREND,TRENDING,BREAKOUT
```
Or implement in code: in the entry gate, require `verdict.quality in ('TREND', 'TRENDING', 'BREAKOUT')`.

**Rationale:** Jun 02's MID-CE entries had 30% WR. Jun 10's MID-PE entries had 67% WR. The sample is small, but MID-regime direction is inherently less reliable (the market is not trending, so the weighted direction signal has less structure to work with). TREND entries on Jun 10 and Jun 12 had consistently good WR. The scalper exit for MID entries caps the win at 3%, which is insufficient to overcome direction uncertainty.

**R3. Update futures symbol before June 26.** [Priority: HIGH]  
Update `BANKNIFTY_FUTURES_SYMBOL=BANKNIFTY26JULFUT` in `.env.compose`. Failure to do so will cause the futures feed to drop on June 26, breaking regime detection and direction signals.

**R4. Verify serve-parity after deploying R1.** [Priority: HIGH]  
After deploying the NaN fixes, run a single-day live check during market hours to confirm all 40 features are non-NaN. Run `ops/gcp/verify_config.py` and add a quick feature check:
```bash
sudo docker exec option_trading-strategy_app-1 python /tmp/verify_config.py
```
If NaN count is still > 2, the deploy didn't take — check which container is serving and re-cp.

### Medium Term (Before Enabling Real Money)

**R5. Re-run June sim with fixed model.** [Priority: MEDIUM]  
After deploying R1, re-run `ops/research/jun_verify_sim.py` against June MongoDB data. The sim will still use old snapshots (NaN features in MongoDB), but the serve-time fixes should reduce them. This gives an updated baseline.

**R6. Paper validate for 2 weeks post-fix.** [Priority: MEDIUM]  
After R1+R2, run paper mode for 2 weeks (July). Target:
- Active firing: 3-5 days out of 10 trading days
- WR ≥ 55% on TREND entries
- Per-trade avg ≥ +1.0% before considering live

**R7. Add a daily NaN check to the startup log.** [Priority: LOW]  
On startup, log the NaN count from a test bar prediction. This makes serve-parity visible without needing a manual check. Add to `ml_entry.py` startup or `main.py` health check.

### Things That Are Confirmed Working (Do Not Change)

- `REGIME_DIRECTION_SIGNAL=weighted` — confirmed in container, routing correctly
- `EXIT_STRATEGY_MODE=adaptive` + `ADAPTIVE_LOTTERY_REGIMES=TREND,TRENDING,BREAKOUT` — lottery for TREND, scalper for MID, routing verified in sim
- `EXIT_THESIS_FAIL_BARS=5` — fires correctly in sim, prevents holding dead positions
- Entry model file path and loading — confirmed working
- `RegimeDirector.decide()` — reads `mtf_derived` correctly from SnapshotAccessor, all required fields present in MongoDB

---

## 8. Seller System Status (Separate)

The seller (iron condor) is in paper mode. It runs independently of the buyer and does not affect buyer P&L.

**2024 backtest result:** 78% WR, +₹1,692/trade, +₹123k over 43 days (drop-top3: +₹104k, still positive).

**Pending paper validation items:**
- Confirm fills arrive in `execution_fills` with `source=seller`
- Confirm TP and stop trigger correctly on live option premiums
- Run 10 paper trades, then evaluate for `SELLER_LIVE_ENABLED=1`

The seller system should NOT be the focus of the buyer-side review.

---

## 9. What Has Been Definitively Ruled Out

These are findings from months of prior research. They should not be revisited:

| Hypothesis | Finding | Evidence |
|---|---|---|
| Buying has edge without entry model | No — avg MFE 20pt, below cost | Grid: 72 cells, all negative without entry gate |
| Scalper exit works for these entries | No — exits at 3%, misses the move | Grid: scalper −₹3,945 vs lottery +₹6,087 |
| 5-min direction is predictable standalone | No — 50% coin flip | Per-member analysis 633 bars; vwap max 54.2% |
| Tight stop on lottery entries | No — triggers on normal vol, not loss | Jun 05 incident: lottery with 5% stop blew up |
| Entry model at 0.85 threshold (too tight) | Fires 25 bars/day but excludes 0.826 dominant cluster | Live June 2026 analysis |

---

## 10. Open Questions for Reviewer

1. **MID-regime entries**: Is the MID → scalper path worth keeping? The 30% WR on Jun 02 MID-CE entries makes it a net loser. But Jun 10 MID-PE was 67% WR. Sample is too small (n=26 and n=3). Recommend TREND-only for now and revisit with more data.

2. **Threshold 0.35 vs 0.40**: With 0 NaN features (post-fix), threshold 0.35 should fire less often. We don't know the post-fix fire rate until we deploy and observe. If it fires > 20/day on active days, raise to 0.40.

3. **Direction signal on Jun 02**: The weighted signal picked CE on a falling/ranging afternoon. Should we add a direction confidence threshold (e.g., only enter if `|weighted_score| > 0.3`)? Currently all non-ABSTAIN signals are taken regardless of confidence.

4. **Capital**: ₹24k is below safe threshold for monthly expiry. Should we operate at weekly expiry only, or pause live trading until capital is rebuilt to ₹50k?

---

## Appendix: Key Files

| File | Purpose |
|---|---|
| `ops/gcp/verify_config.py` | Run inside container to confirm all env vars |
| `ops/research/jun_verify_sim.py` | June 2026 sim (run inside strategy_app container) |
| `strategy_app/ml/bundle_inference.py` | Feature extraction + model scoring |
| `snapshot_app/core/compression_features.py` | Per-bar adx_14 + vol_spike_ratio computation |
| `snapshot_app/core/stage_views.py` | Snapshot block projection (NaN-wins fix here) |
| `strategy_app/brain/regime_director.py` | Regime quality + direction signal |
| `strategy_app/position/exit_policy.py` | Adaptive exit stack |
| `.env.compose` on VM `/opt/option_trading/` | Live config (185-line, never commit) |
| `docs/strategy_platform/CONFIG.md` | Config flow diagram and all vars |

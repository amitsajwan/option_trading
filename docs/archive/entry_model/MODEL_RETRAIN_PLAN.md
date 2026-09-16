# Model Retrain Plan — BankNifty + NIFTY

## 1. What Is Wrong Now

### 1a. Missing Medians (Critical)
The bundle `joblib` has `medians = {}` for entry models.  
At inference: NaN feature → passes raw NaN to XGBoost → model produces garbage or refuses.  
Fix: training pipeline must compute `median per feature` from training set and save in bundle.

### 1b. VIX Always NaN in Live (High)
`vix_current` and `vix_intraday_chg` are NaN every bar because Dhan WebSocket returns HTTP 429.  
These are 2 of 75 direction model features. The model runs (dir_nan=10 tolerance) but with 0s.  
Fix options: (a) Poll VIX via REST `/v2/marketfeed/quote` instead of WS, (b) Remove from model.

### 1c. iv_pct_rank_session Proxy Mismatch (Medium)
Training: rolling intraday IV percentile (changes bar-by-bar).  
Live serving: historical iv_percentile from snapshot_app (single daily value, different scale).  
Fix: implement `iv_pct_rank_session` in `live_velocity_state.py` as a running session accumulator.

### 1d. NIFTY 9-Feature Model Unknown (High)
- Files only on VM, not in git repo — will be lost on rebuild
- Feature list unknown — cannot audit training vs serving parity
- No medians

---

## 2. Feature Sources — What Comes From Where

All features are derived from two raw data sources:

| Source | Live | Replay | Training |
|--------|------|--------|----------|
| 1-min OHLCV (index/futures) | Dhan WS tick → ingestion_app OHLC redis | `/charts/intraday` via ingestion_app historical | Same Dhan API |
| Per-strike OI/IV/Volume | Dhan option chain (every bar poll) | `/charts/rollingoption` via ingestion_app historical | Same Dhan API |

**Training and serving use the same Dhan API source. Feature computation must be identical.**

### Feature Groups

#### GROUP A — Price / EMA (always available, no NaN risk)
| Feature | Source | Warmup |
|---------|--------|--------|
| `ema_9_slope`, `ema_21_slope`, `ema_50_slope` | OHLCV | 50 bars |
| `ema_spread_9_21`, `ema_spread_21_50` | OHLCV | 50 bars |
| `ema_order` | OHLCV | 50 bars |
| `adx_14` | OHLCV | 28 bars |
| `atr_ratio` | OHLCV | 14 bars |
| `realized_vol_30m` | OHLCV | 30 bars |
| `bb_width_20`, `bb_width_chg_5` | OHLCV | 25 bars |
| `vel_price_delta_open/30m/60m`, `vel_price_acceleration` | OHLCV | 60 bars |
| `compression_score`, `range_10`, `range_30`, `range_ratio_10_30` | OHLCV | 30 bars |
| `candle_overlap_10` | OHLCV | 10 bars |

#### GROUP B — Options OI/IV/Volume (requires option chain at each bar)
| Feature | Source | Warmup | NaN Risk |
|---------|--------|--------|----------|
| `atm_ce_oi`, `atm_pe_oi`, `atm_oi_ratio`, `near_atm_oi_ratio` | Option chain | 1 bar | Low — chain always present |
| `pcr`, `pcr_change_5m/15m/30m` | Option chain | 30 bars | Low |
| `atm_ce_iv`, `atm_pe_iv`, `atm_iv`, `iv_skew` | Option chain | 1 bar | Low |
| `vel_ce_oi_delta_open/30m`, `vel_pe_oi_delta_open/30m` | Session accumulator | 30 bars | None after warmup |
| `vel_oi_ratio_delta_open/30m`, `vel_ce/pe_oi_build_rate` | Session accumulator | 30 bars | None |
| `vel_pcr_delta_open/30m`, `vel_pcr_acceleration`, `vel_pcr_trend_direction` | Session accumulator | 30 bars | None |
| `vel_atm_ce/pe_iv_delta_open`, `vel_iv_skew_delta_open`, `vel_iv_compression_rate` | Session accumulator | 30 bars | None |
| `vel_ce/pe_vol_delta_30m` | Session accumulator | 30 bars | Low |
| `vel_options_vol_acceleration` | Session accumulator | 60 bars | **Medium** — requires 60 bars of option volume |

#### GROUP C — VIX (unreliable in live, NaN from WS 429)
| Feature | Source | NaN Risk |
|---------|--------|----------|
| `vix_current` | Dhan WS (rate limited) | **HIGH** — NaN most bars |
| `vix_intraday_chg` | Computed from vix_current | **HIGH** — NaN most bars |

**Decision**: Exclude VIX from entry model. For direction model: either fix WS rate limit or use REST poll fallback.

#### GROUP D — Session Context (require prev_day data + AM session 9:15-11:30)
| Feature | Source | NaN Risk |
|---------|--------|----------|
| `ctx_am_gap_from_yday`, `ctx_gap_pct`, `ctx_gap_up/down`, `ctx_am_gap_filled` | prev_day OHLCV | Fixed — prev_day fetch now works |
| `ctx_am_range_high/low/size`, `ctx_am_price_position` | AM session (9:15-11:30) | Available after 11:30 |
| `ctx_am_trend`, `ctx_am_trend_strength`, `ctx_am_reversal` | AM session | Available after 11:30 |
| `ctx_am_oi_direction`, `ctx_am_vwap_side`, `ctx_am_breakout_confirmed` | AM session + options | Available after 11:30 |

#### GROUP E — Session Time (always available)
| Feature | Source |
|---------|--------|
| `minute_of_day`, `minutes_to_close`, `day_of_week` | Timestamp |
| `minute_index` (alias: `minutes_since_open`) | Timestamp |
| `dte_days`, `is_expiry_day` | Option chain expiry |
| `opening_range_ready`, `opening_range_breakout_up/down` | ORB tracker |
| `position_in_day_range` | OHLCV + daily range |

---

## 3. Proposed Feature Sets for Retrain

### 3a. BankNifty Entry Model (predict: ≥100pt move in 15min?)

**Keep (38 features, remove VIX):**
- All GROUP A price features (12 features)
- GROUP B options features EXCEPT `vel_options_vol_acceleration` which is brittle — use simpler proxy (8 vel + 4 PCR vel + 4 IV vel + 2 vol vel = 18)
- GROUP D AM context (8 features, all available after warmup)
- `minute_index`, `dte_days` from GROUP E

**Remove:**
- `vix_current`, `vix_intraday_chg` (unreliable WS)

**Total: ~38 features, all reliably available**

**Required medians**: Compute from training set for ALL features. Set `max_nan_features=3`.

### 3b. NIFTY Entry Model

**Same feature set as BN Entry (38 features), trained on NIFTY data.**

Rationale:
- NIFTY and BN have same market microstructure (same Dhan data format)
- NIFTY has weekly expiry (shorter DTE range 0-7) vs BN monthly (DTE 7-30+)
- `dte_days` captures this difference automatically
- Training on NIFTY-specific data handles the different volatility/gamma profile

**Current 9-feature NIFTY model is too small** — likely misses the options dynamics that drive moves.

### 3c. BankNifty Direction Model (predict: CE or PE side?)

**Keep (73 features, remove VIX, fix iv_pct_rank):**
- Remove: `vix_current`, `vix_intraday_chg`
- Fix: `iv_pct_rank_session` → compute as rolling session percentile in `live_velocity_state.py`
- Rest unchanged (IV skew, PCR, OI, price momentum, session context, ORB, DTE)

### 3d. NIFTY Direction Model

**Same 73-feature set as BN Direction, trained on NIFTY data.**

---

## 4. Model Contract Definition (to implement)

Each bundle must contain:

```python
{
  "kind": "entry_only_bundle" | "direction_only_bundle",
  "instrument": "BANKNIFTY" | "NIFTY",
  "version": "2.0",
  "features": [...],          # ordered list — inference uses this exact order
  "medians": {                # for NaN imputation at inference time
      "ema_9_slope": 0.00012,
      "vel_ce_oi_delta_open": 0.023,
      ...
  },
  "feature_sources": {        # which data source each feature needs
      "ema_9_slope": "price_ohlc",
      "vel_ce_oi_delta_open": "options_chain",
      "ctx_am_gap_from_yday": "prev_day_close",
      "vix_current": "vix_ws",  # if included
      ...
  },
  "max_nan_features": 3,      # saved in bundle, read at inference time
  "training_date_range": "2024-11-01 to 2026-06-30",
  "training_regime": "monthly",
  "label_definition": "binary: 1 if |move| >= 100pt in 15min else 0",
  "holdout_eval": {
      "roc_auc": 0.71,
      "date_range": "2026-04-01 to 2026-06-30",
      "n_samples": 5420
  }
}
```

---

## 5. Parity Test (to add to CI)

After training, auto-run:
```python
# For each feature in bundle["features"]:
# 1. Build a live snapshot (from replay or live)
# 2. Extract via project_stage_views_v2()
# 3. Verify feature name exists and value is finite
# 4. Compare distribution to training distribution (mean, std within 3σ)
```

This becomes the CI gate before any model can be deployed.

---

## 6. Execution Plan

### Step 1 — Fix existing bundles (no retrain, 1 day)
- Add `medians` to existing BN entry bundle (compute from training data)
- Copy NIFTY models to repo, audit 9-feature list
- Fix `max_nan_features` defaults

### Step 2 — Fix iv_pct_rank_session (1 day)
- Implement rolling session IV percentile in `live_velocity_state.py`
- Add to `project_stage_views_v2` output
- Verify parity between training and live computation

### Step 3 — Retrain BN Entry (2 days)
- Remove VIX features, keep 38-feature set
- Add median computation to training pipeline
- Train on Nov2024-Jun2026 Dhan data (monthly regime)
- Run parity test, check holdout AUC (expect ~0.71)
- Bundle with full contract metadata

### Step 4 — Retrain NIFTY Entry (1 day)
- Same 38-feature pipeline
- Train on NIFTY Dhan data (weekly expiry regime)
- Replace 9-feature model

### Step 5 — Retrain Direction Models (2 days, both instruments)
- 73-feature set (remove VIX)
- With fixed iv_pct_rank_session
- Separate models for BN and NIFTY

### Total: ~7 days of focused work

---

## 7. Open Questions for Decision

1. **VIX**: Remove entirely or fix the WS rate limit?
   - Fixing WS: need to subscribe to VIX in its own WebSocket slot, not shared
   - Removing: simpler, loses market fear signal (VIX did predict some AVOID regimes)
   - **Recommendation**: Fix WS rate limit first (it's a config change), keep VIX

2. **NIFTY 9-feature model**: Replace with 38-feature or keep and improve?
   - 9 features is almost certainly undertrained — can't capture options dynamics
   - **Recommendation**: Replace with 38-feature architecture

3. **Entry label**: ≥100pt in 15min vs something else?
   - 100pt = ~0.2% for NIFTY at 50k, ~0.17% for BN at 58k
   - Current label seems right for capturing meaningful intraday moves
   - **Recommendation**: Keep, but measure actual hit rate from replay data first

4. **Shared vs separate direction models**?
   - BN and NIFTY have different gamma profiles (weekly vs monthly expiry)
   - **Recommendation**: Keep separate models, same feature set

# Direction Model Feature Verification & Gap Fix

**Owner:** Any team (Windsurf / Codex / Claude Code)  
**Branch:** `feat/dhan-feature-engine`  
**Last verified:** 2026-06-30  
**Status:** 72/75 features real. 3 gaps to fix.

---

## Context

The NIFTY and BankNifty direction models each use 75 features. After fixing name-mismatch aliases in `project_stage2_direction_view_v2` (snapshot_app/core/stage_views.py), 72/75 features now extract correctly from live snapshots. The same 3 features are missing for both instruments:

| Feature | Why Missing | Priority |
|---------|------------|----------|
| `vix_current` | Dhan WS returning HTTP 429 (rate limit) for VIX subscription | **P1 — fix** |
| `vix_intraday_chg` | Same | **P1 — fix** |
| `iv_pct_rank_session` | Requires rolling session IV percentile — not computed live | **P2 — implement** |

Both models are producing real predictions with medians for these 3. Direction model working — but VIX is a meaningful feature (model trained on it). Fix these to get to 75/75.

---

## Verification Procedure

Run this on the runtime VM to verify feature coverage for any direction model:

```bash
# On runtime VM — copy verify script into container and run
sudo docker cp /tmp/verify_direction.py option_trading-strategy_app_nifty-1:/tmp/
sudo docker exec option_trading-strategy_app_nifty-1 python /tmp/verify_direction.py
```

The verify script (`/tmp/verify_direction.py`) does:
1. Loads the direction model bundle from `/app/models/`
2. Gets the latest live snapshot from `/app/.run/snapshot_app_nifty/events.jsonl`
3. Calls `_build_feature_row(snap, feats)` — the exact same path as live inference
4. Reports: NaN count, which features are missing, CE prob, direction

**Expected output (healthy state):**
```
NaN (median-filled): 3 / 75  ->  Real: 72 / 75
Remaining NaN: ['iv_pct_rank_session', 'vix_intraday_chg', 'vix_current']
CE prob: 0.XXXX
Direction: CE or PE  (confidence: 0.XXX)
```

**Failure indicators:**
- `CE prob: None` → `_build_feature_row` is returning None → direction model falling back to momentum
- NaN count > 3 → regression, some alias resolution broke
- NaN count = 3 but CE prob = 0.5 exactly → model output degenerate

---

## Fix 1: VIX Feed (P1)

**Root cause:** The Dhan WebSocket feed is getting HTTP 429 (rate limited) when subscribing to VIX (security_id="21", segment=IDX_I). The ingestion_app's `DhanWsFeed` subscribes to VIX alongside the main index, but Dhan rate-limits simultaneous WS connections.

**Where:** `ingestion_app/dhan_ws_feed.py` — `_instruments()` method.

**What to check:**
```bash
# Check current WS subscription errors
sudo docker logs --since 1h option_trading-ingestion_app-1 2>&1 | grep -E "429|VIX|WS|error" | head -10
sudo docker logs --since 1h option_trading-ingestion_app_nifty-1 2>&1 | grep -E "429|VIX|WS|error" | head -10
```

**Fix options (pick one):**
1. **Retry with backoff** — when WS gets 429, wait 30s before reconnecting. Already has reconnect logic but no delay on 429.
2. **Subscribe VIX separately with delay** — subscribe main index first, then VIX 10s later to avoid simultaneous connections.
3. **REST fallback for VIX** — if WS VIX is None, call `svc.get_tick("INDIAVIX")` via REST every bar. Slower but reliable.

**Relevant code:**
- `ingestion_app/dhan_ws_feed.py` — `DhanWsFeed._run_loop()` — reconnect on crash
- `ingestion_app/dhan_ws_feed.py` — `_instruments()` — subscription list
- `ingestion_app/dhan_data_service.py` — `DhanDataService.__init__()` — WS feed init

**Verification after fix:**
```python
# In ingestion container:
from ingestion_app.api_service import svc
tick = svc.get_tick("INDIAVIX")
print("VIX last_price:", tick.get("last_price"))  # Should be a real number, not nan
```

Then re-run the direction model verify script — `vix_current` and `vix_intraday_chg` should show as OK.

---

## Fix 2: iv_pct_rank_session (P2)

**Root cause:** `iv_pct_rank_session` is the ATM IV percentile rank within the current session (0=lowest IV of day, 1=highest). It requires maintaining a rolling list of ATM IV values since 09:15 IST. This state is not maintained in the live snapshot_app.

**Where to add:** `snapshot_app/core/market_snapshot.py` or `snapshot_app/core/live_ml_flat.py` — where the snapshot is built each bar.

**What it should compute:**
```python
# Each bar:
session_iv_history.append(current_atm_iv)
iv_pct_rank_session = sum(1 for v in session_iv_history if v <= current_atm_iv) / len(session_iv_history)
# Range: 0.0 to 1.0. Reset at session start (09:15 IST each day).
```

**Where the state lives:** `snapshot_app/core/live_velocity_state.py` already maintains rolling session state (e.g. velocity accumulators). Add `iv_pct_rank_session` to the same pattern.

**Where to write it into snapshot:** In `snapshot_app/core/market_snapshot.py` or `live_ml_flat.py`, add it to `iv_derived` block.

**Field name:** `iv_pct_rank_session` (model expects this exact name — already read from `iv_derived` in `project_stage2_direction_view_v2`).

**Verification after fix:**
```bash
# Check snapshot has the field populated
sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --eval "
var s=db.phase1_market_snapshots.findOne({},null,{sort:{_id:-1}});
print(s.payload.snapshot.iv_derived.iv_pct_rank_session);
"
# Should print a float between 0 and 1, not null
```

---

## How to Run the Full Verification

```bash
# 1. SSH to runtime VM
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b

# 2. Copy verify script into both strategy containers
sudo docker cp /tmp/verify_direction.py option_trading-strategy_app_nifty-1:/tmp/
sudo docker cp /tmp/verify_direction.py option_trading-strategy_app-1:/tmp/

# Modify script for BankNifty (change model path and events.jsonl path):
# bundle = joblib.load("/app/models/direction_monthly_v2.joblib")
# lines = Path("/app/.run/snapshot_app/events.jsonl").read_text()...

# 3. Run for NIFTY
sudo docker exec option_trading-strategy_app_nifty-1 python /tmp/verify_direction.py

# 4. Run for BankNifty
sudo docker exec option_trading-strategy_app-1 python /tmp/verify_direction_bn.py

# 5. Expected: both show Real: 72/75, CE prob != None, direction = CE or PE
# After VIX fix: Real: 74/75
# After iv_pct_rank_session fix: Real: 75/75
```

---

## What NOT to do

- **Do not add fallback extraction** in `direction_ml_policy._build_feature_row` — `project_stage_views_v2` works and is importable. Fallbacks hide bugs.
- **Do not retrain the model** to remove VIX features — VIX is genuinely useful signal. Fix the feed.
- **Do not change the feature names** in the model — names like `dte_days`, `minute_of_day` etc are already handled by the alias resolution in `project_stage2_direction_view_v2`. Adding more aliases there is the right pattern.

---

## Files Changed (as reference)

| File | Change | Why |
|------|--------|-----|
| `snapshot_app/core/stage_views.py` | Added alias resolution in `project_stage2_direction_view_v2` | 6 features had name mismatches between training and live |
| `strategy_app/ml/direction_ml_policy.py` | Changed `except` level from `debug` → `warning` | Make failures visible instead of silent |

---

## Model Quality Baseline

As of 2026-06-30 with 72/75 features:

| Model | Features | AUC (holdout) | Live CE prob range | Status |
|-------|----------|---------------|-------------------|--------|
| BankNifty direction (`direction_monthly_v2`) | 75 | 0.71 | Varies, not degenerate | ✅ Working |
| NIFTY direction (`nifty_direction_v2`) | 75 | 0.64 | Varies, not degenerate | ✅ Working |

**Degenerate check:** if CE prob is always exactly 0.5, or always the same value every bar, the model is not using features (extraction failure). Check with:
```bash
# Watch CE prob on 5 consecutive bars
sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --eval "
db.strategy_votes_nifty.find({trade_date_ist:new Date().toISOString().split('T')[0],signal_type:'ENTRY'},
  {timestamp:1,ml_ce_prob:1,ml_pe_prob:1,_id:0}).sort({_id:-1}).limit(5).toArray()
  .forEach(v=>print(v.timestamp.substring(11,16), 'ce='+v.ml_ce_prob, 'pe='+v.ml_pe_prob));
"
```
`ml_ce_prob=null` → direction model not scoring (extraction failure). Real values varying bar-to-bar → working correctly.

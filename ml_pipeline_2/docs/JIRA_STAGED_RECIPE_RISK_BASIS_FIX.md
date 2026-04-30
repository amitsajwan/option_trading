---
title: ML_PURE_STAGED Recipe Risk Basis Fix - Trades Exiting at 1m via Option-Premium Stop Instead of Underlying Stop
type: Bug Fix + Deployment
priority: High
component: strategy_app, ml_pipeline_2, dashboard
labels: staged-runtime, risk-basis, stop-loss, live-trading
---

## Problem Statement

Live trading with `ML_PURE_STAGED` engine shows all trades exiting at **1-minute hold** with tiny PnL (-0.02% to -6.09%). The stop is being calculated from **option premium** instead of **BANKNIFTY futures price**.

### Evidence from Live Trades

```
Time    Strategy         Dir   Entry     Exit      PnL      Hold
09:51   ML_PURE_STAGED   LONG  117.20    115.35   -0.02%    1m
09:49   ML_PURE_STAGED   LONG  130.50    122.55   -0.06%    1m
09:47   ML_PURE_STAGED   LONG  122.75    117.00   -0.05%    1m

Exit Logic:
STOP_LOSS pnl=-6.09% mfe=0.00% mae=-6.09% stop=130.37
stop_loss_pct: 0.10%
target_pct: 0.25%
max_hold_bars: 25
stop_price: 130.3695
```

**Root Cause:** Recipe `L6` has `stop_loss_pct=0.001` (0.1%). This was designed for **underlying-scale** risk (BANKNIFTY futures: 0.1% = ~50 points), but the runtime is applying it as **option-premium-scale** risk (130.50 × 0.001 = 0.13 point stop on CE option).

---

## Technical Analysis

### Data Flow (Where the Bug Lives)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  STAGE 1-3 PIPELINE (ml_pipeline_2)                                         │
│  ─────────────────────────────────                                          │
│  Recipe Catalog:                                                            │
│    L6: horizon_minutes=25, take_profit_pct=0.0025, stop_loss_pct=0.0010    │
│         ↑ TINY VALUES (0.08%-0.25%)                                        │
│         ↑ These are BANKNIFTY-scale percentages (underlying)                 │
│                                                                              │
│  ↓ Published to runtime bundle                                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  RUNTIME (strategy_app/engines/pure_ml_staged_runtime.py)                    │
│  ────────────────────────────────────────────────────────                     │
│  OLD BUG (line 332):                                                       │
│    risk_basis = recipe_meta.get("risk_basis") or "option_premium"           │
│         ↑ Legacy bundles without risk_basis field default to premium!      │
│                                                                              │
│  ↓ Builds StagedRuntimeDecision                                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  SIGNAL BUILDER (strategy_app/engines/trade_signal_builder.py)              │
│  ────────────────────────────────────────────────                            │
│  if risk_basis == "underlying":                                            │
│      underlying_stop_pct = recipe.stop_loss_pct  ← CORRECT PATH          │
│      stop_loss_pct = 0.0                                                      │
│  else:                                                                        │
│      stop_loss_pct = recipe.stop_loss_pct          ← BUG PATH (current)    │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  POSITION FACTORY (strategy_app/position/position_factory.py)               │
│  ────────────────────────────────────────────────                            │
│  stop_price = entry_premium * (1 - stop_loss_pct)                          │
│  # With stop_loss_pct=0.001 and entry=130.50:                               │
│  # stop_price = 130.37 (0.13 point stop = INSTANT HIT)                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Attempted Fixes (Committed but Not Deployed)

### Fix 1: Auto-Detect Underlying for Tiny Percentages
**File:** `strategy_app/engines/pure_ml_staged_runtime.py:318-328`

```python
# NEW LOGIC (committed, needs deploy)
raw_stop = float(recipe_meta.get("stop_loss_pct") or 0.0)
raw_target = float(recipe_meta.get("take_profit_pct") or 0.0)
explicit_basis = str(recipe_meta.get("risk_basis") or "").strip().lower()
if explicit_basis in ("underlying", "option_premium"):
    risk_basis = explicit_basis
elif max(abs(raw_stop), abs(raw_target)) <= 0.01:  # ≤1% implies underlying
    risk_basis = "underlying"  # ← KEY FIX
else:
    risk_basis = "option_premium"
```

**Why:** Legacy runtime bundles (published before `risk_basis` field existed) were defaulting to `option_premium`. Tiny percentages (0.08%-0.25%) are impossible for option-premium stops — they only make sense for underlying-scale risk.

### Fix 2: Add Exit Reason Visibility to Dashboard
**Files:**
- `strategy_app/position/tracker.py` - Added `underlying_stop_pct`, `underlying_target_pct`, `entry_futures_price` to closed position records
- `market_data_dashboard/static/redesign/components.js` - Added "Exit Reason" column to trade table
- `market_data_dashboard/static/redesign/api.js` - Mapped `exit_reason` field

---

## Deployment Status

| Component | Status | Location |
|-----------|--------|----------|
| Code committed | ✅ Done | `chore/ml-pipeline-ubuntu-gcp-runbook` |
| Tests passing | ✅ Done | `strategy_app/tests/test_pure_ml_staged_engine.py` |
| Pushed to GitHub | ✅ Done | Origin |
| Pulled on GCP VM | ❌ **BLOCKED** | Permission denied on `/opt/option_trading` |
| Docker rebuild | ❌ Pending | `strategy_app` and `dashboard` containers |
| Live verification | ❌ Pending | Check trades show "Exit Reason" column |

---

## Manual Deploy Commands (Assignee Should Run)

```bash
# SSH to GCP VM
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b

# On VM:
cd /opt/option_trading
sudo git pull origin chore/ml-pipeline-ubuntu-gcp-runbook

# Rebuild containers
sudo docker compose -f docker-compose.yml build strategy_app dashboard
sudo docker compose -f docker-compose.yml up -d strategy_app dashboard

# Verify
sudo docker compose -f docker-compose.yml ps
curl -fsS "http://127.0.0.1:8008/api/health" | python3 -m json.tool
```

---

## Verification Criteria

### Before Fix (Current State)
- Trade hold time: **1 minute**
- Exit reason: **STOP_LOSS** (option-premium stop)
- `stop_price` = `entry_premium * (1 - 0.001)`
- Dashboard: No "Exit Reason" column visible

### After Fix (Expected)
- Trade hold time: **5-25 minutes** (matches `max_hold_bars`)
- Exit reason: **STOP_LOSS** (but now underlying-scale) or **TARGET_HIT**
- `underlying_stop_pct` = 0.001 (0.1% of BANKNIFTY futures = ~50 points)
- Dashboard: "Exit Reason" column visible in trade table
- Log shows: `risk_basis=underlying`

---

## Related Files

| File | Purpose |
|------|---------|
| `strategy_app/engines/pure_ml_staged_runtime.py:318-328` | **CRITICAL FIX** - Risk basis auto-detection |
| `strategy_app/engines/trade_signal_builder.py:35-47` | Routes risk to underlying_* fields |
| `strategy_app/position/position_factory.py:44-47` | Builds position from signal |
| `strategy_app/position/tracker.py:96-152` | Exit logic with underlying stop check |
| `ml_pipeline_2/src/ml_pipeline_2/staged/recipes.py:12-24` | Recipe catalog definitions |
| `market_data_dashboard/static/redesign/components.js:102-131` | Trade table UI |

---

## Additional Notes

1. **Historical Context:** The staged recipe catalog was designed for BANKNIFTY/base-instrument behavior modeling. The percentages (0.08%-0.25%) represent reasonable intraday moves on the underlying (50-125 points), not option premium moves.

2. **Why Not Just Widen Stops?** Option premium volatility is ~20-50% intraday. If we kept `risk_basis=option_premium`, we'd need stops like 15-20% (0.15-0.20) to avoid noise — which breaks the model's training assumptions.

3. **Risk Guard:** `runtime_contract.py:57-61` now rejects `option_premium` recipes with stops < 1% to prevent this regression.

---

## Assignee Checklist

- [ ] Run manual deploy commands on `option-trading-runtime-01`
- [ ] Verify container restarts successfully
- [ ] Check dashboard shows "Exit Reason" column
- [ ] Monitor next live session for >1m hold times
- [ ] Confirm `risk_basis=underlying` appears in strategy logs

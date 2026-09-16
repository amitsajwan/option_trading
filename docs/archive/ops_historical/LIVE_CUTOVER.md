# Live Cutover Runbook
*Pre-production → Production. Requires: 5-day shadow sign-off by trader.*

---

## Prerequisites (all must be ticked before starting)

```
[ ] Shadow mode ran for 5+ consecutive trading days
[ ] Slippage < 0.15% of premium (measured from execution:fills:real:v1 vs :paper:v1)
[ ] Exit stack enabled and behaviour confirmed paper-mode
[ ] CONSENSUS_BYPASS_MIN_CONFIDENCE=0.65 deployed and verified
[ ] run_id in MongoDB positions confirmed (restart strategy_app, check new run appears)
[ ] Kite access token auto-refresh tested (manual dry-run of ingestion_app.token_refresh)
[ ] Telegram alerts firing correctly (paper position open/close appearing in Telegram)
[ ] Daily P&L report generating at 15:40 IST (check docs/reports/ for yesterday's file)
[ ] GCP VM uptime monitoring active (Cloud Monitoring alert on CPU=0 or process absent)
[ ] MongoDB backup confirmed (mongodump or Atlas daily snapshot)
[ ] Trader has reviewed shadow results and given written sign-off
```

---

## Step 1 — Verify shadow P&L side-by-side

```bash
# On GCP VM — check last 5 days slippage
redis-cli XRANGE execution:fills:real:v1 - + COUNT 100 | \
  python3 -c "
import sys, json
data = sys.stdin.read()
# parse and compute avg slippage
print('see execution:fills:real:v1 and execution:fills:paper:v1 streams')
"

# Dashboard: switch to shadow comparison view
# http://34.93.40.198:8008/app/ → toggle 'Shadow Mode' panel
```

---

## Step 2 — Switch EXECUTION_ADAPTER to kite

Edit `.env` on GCP VM:

```bash
# BEFORE
EXECUTION_ADAPTER=shadow
SHADOW_MAX_LOTS=1

# AFTER
EXECUTION_ADAPTER=kite
# No SHADOW_MAX_LOTS needed in kite mode
```

Keep size multiplier at 0.25 for the first 2 weeks:
```bash
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ROLLOUT_STAGE=capped_live
```

Restart execution_app only (strategy_app continues uninterrupted):
```bash
cd /opt/option_trading
sudo docker compose --profile live up -d --no-deps execution_app
sudo docker logs -f option_trading-execution_app-1 --tail 50
```

---

## Step 3 — Verify first live order

Watch health and fills:
```bash
# Health check
curl http://localhost:8009/health
# Expected: {"status":"ok","adapter":"kite"}

# Watch fills stream
redis-cli XREAD COUNT 1 STREAMS execution:fills:v1 0
# When next trade fires, you should see status=filled with a real fill_price
```

Check Kite positions portal (app.zerodha.com → Portfolio → Positions) to confirm order appears.

---

## Step 4 — Confirm P&L reconciliation

After the first real trade closes:
```bash
# MongoDB strategy_positions should have fill_entry_price and fill_exit_price
python3 -c "
from pymongo import MongoClient
c = MongoClient('localhost', 27017)
docs = list(c.trading_ai.strategy_positions.find(
    {'event': 'POSITION_CLOSE', 'fill_pnl_pct': {'\$exists': True}},
    {'position_id': 1, 'fill_entry_price': 1, 'fill_exit_price': 1, 'fill_pnl_pct': 1}
).sort('timestamp', -1).limit(3))
for d in docs:
    print(d)
"
```

---

## Rollback procedure

One env var change, no code change:
```bash
# On GCP VM
sed -i 's/EXECUTION_ADAPTER=kite/EXECUTION_ADAPTER=paper/' .env
sudo docker compose --profile live up -d --no-deps execution_app
```

Rollback takes < 60 seconds. Any open position is managed by strategy_app normally
(its exits are independent of the execution adapter).

---

## Scale-up schedule (after performance review)

| Week | `STRATEGY_POSITION_SIZE_MULTIPLIER` | Notes |
|---|---|---|
| 1–2 | 0.25 | Capped live — default |
| 3–4 | 0.50 | After 10+ live trades with slippage confirmed |
| 5+ | 1.00 | After trader approval + 30-day drawdown review |

Change only this env var and restart strategy_app:
```bash
STRATEGY_POSITION_SIZE_MULTIPLIER=0.50
sudo docker compose up -d --no-deps strategy_app
```

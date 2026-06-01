# Team Onboarding Guide
*Read this before touching any code. ~30 minutes.*

---

## What This System Does

This is an automated intraday options trading system for BANKNIFTY on NSE. It:

1. Collects live market data from Zerodha Kite (futures bars + options chain)
2. Builds a canonical market snapshot every minute
3. Runs a strategy engine that evaluates whether to BUY a call (CE) or put (PE) option
4. Emits trade signals when conditions are met
5. (Currently paper only — no real orders sent)
6. Tracks positions, P&L, and logs every decision in full detail

The system is **not a black box**. Every decision is traced: which gate fired, which signal drove direction, why the entry probability was high or low. If you can't explain why a trade fired in 60 seconds from the trace, that's a bug.

---

## The Stack

```
Language:  Python 3.12+
Broker:    Zerodha Kite (NSE, India)
Data bus:  Redis (pub/sub + streams)
Storage:   MongoDB + JSONL files
UI:        FastAPI + React JSX (no build step — loaded directly)
Deploy:    Docker Compose on GCP VM
```

---

## First Day Setup

### 1. Access the VM
```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b
```

### 2. See what's running
```bash
sudo docker ps --format "table {{.Names}}\t{{.Status}}"
```

You should see: `strategy_app`, `dashboard`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_persistence_app`, `depth_collector`, `mongo`, `redis`.

### 3. Open the dashboard
Navigate to `http://34.93.40.198:8008/app/` in your browser.

### 4. Watch a live decision
```bash
tail -f /opt/option_trading/.run/strategy_app/decision_traces.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    d = json.loads(line)
    print(d['snapshot_id'], d['final_outcome'], d.get('execution_path', ''))
"
```

---

## How a Trade Happens (follow this flow)

```
1. Kite API delivers 1-minute futures bar + options chain to ingestion_app

2. snapshot_app builds MarketSnapshot:
   - futures_derived.fut_return_5m    ← 5-minute momentum
   - chain_aggregates.pcr             ← put-call ratio
   - strikes[ATM].pe_ltp              ← option price
   - velocity_enrichment              ← OI deltas, IV trend
   - See: snapshot_app/core/market_snapshot.py

3. strategy_app evaluates the snapshot:
   a. Regime classification (BREAKOUT/TRENDING/CHOP/SIDEWAYS)
   b. Direction consensus (shadow signals + momentum + ML hint)
   c. Entry gate (ML confidence threshold)
   d. Policy check (bypass for ML_ENTRY strategy)
   e. Strike selection
   f. Risk check (daily loss, consecutive losses, halt)

4. If all gates pass → TradeSignal emitted to Redis

5. strategy_persistence_app writes to MongoDB

6. Dashboard reads MongoDB + JSONL for display
```

---

## Reading a Decision Trace

Every snapshot evaluation writes one record to `decision_traces.jsonl`. For a fired trade:

```json
{
  "snapshot_id": "20260601_1159",
  "timestamp": "2026-06-01T11:59:00+05:30",
  "final_outcome": "entry_taken",
  "execution_path": "consensus_bypass",

  "regime_context": {
    "regime": "TRENDING",
    "confidence": 0.70,
    "evidence": { "bear_score": 2.2, "bull_score": 0.0, "orl_broken": true }
  },

  "candidates": [{
    "strategy_name": "ML_ENTRY",
    "direction": "PE",
    "confidence": 0.855,
    "terminal_status": "passed",
    "ordered_gates": [
      { "gate_id": "regime_classification", "status": "pass", "metrics": {"regime_confidence": 0.70} },
      { "gate_id": "direction_consensus",   "status": "pass", "message": "PE ce=0.0 pe=3.92 margin=3.92" },
      { "gate_id": "confidence_gate",       "status": "pass", "metrics": {"confidence": 0.855} },
      { "gate_id": "policy_checks",         "status": "pass", "message": "bypass:strategy_owned",
        "metrics": {"entry_prob": 0.855, "entry_threshold": 0.65} },
      { "gate_id": "execution",             "status": "pass", "metrics": {"max_lots": 1} }
    ]
  }]
}
```

**Reading it:**
- `final_outcome: entry_taken` → trade fired
- `execution_path: consensus_bypass` → fired via ML entry probability, not formal candidate selection
- `regime: TRENDING, bear=2.2, bull=0.0` → strongly bearish
- `direction PE, margin=3.92` → clear PE signal (7 shadow signals, no CE signals)
- `entry_prob: 0.855 ≥ threshold 0.65` → confidence sufficient

If `policy_checks: blocked` — the trace has a lie only for pre-2026-06-01 records. After that date, `blocked` means genuinely blocked.

---

## The Two Engines

| Engine | When used | Key behaviour |
|---|---|---|
| `deterministic` | Today, all live trading | Rules + ML consensus; bypass policy; full trace |
| `ml_pure` | Historical backtest / evaluation | Staged ML model; hard deterministic gates |

The `deterministic` engine is confusingly named — it uses ML for entry timing and direction. "Deterministic" refers to the rule-based strategy framework it uses, not that it's ML-free.

---

## Key Files

| What you need | Where |
|---|---|
| Entry/exit/direction logic | `strategy_app/engines/deterministic_rule_engine.py` |
| Direction consensus (with regime veto) | `strategy_app/engines/direction_consensus.py` |
| Exit policies | `strategy_app/position/tracker.py` (TimestopPolicy + others) |
| Position tracking | `strategy_app/position/tracker.py` |
| Risk manager | `strategy_app/risk/manager.py` |
| Regime classifier | `strategy_app/market/regime.py` |
| Snapshot reader | `strategy_app/market/snapshot_accessor.py` |
| Signal logger | `strategy_app/logging/signal_logger.py` |
| Dashboard backend | `market_data_dashboard/routes/` |
| Dashboard frontend | `market_data_dashboard/static/webapp/terminal-live.jsx` |
| Live session reader | `market_data_dashboard/real_source.py` |

---

## Common Debug Patterns

### "Trade fired but dashboard shows nothing"
Boundary check (each takes 30 seconds):
1. `tail /opt/option_trading/.run/strategy_app/positions.jsonl` — POSITION_OPEN exists?
2. Check MongoDB `strategy_positions` — doc exists with today's date?
3. `curl http://localhost:8008/api/v1/monitor/snapshot?mode=live | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['session']['trades'])"` — in session?
4. Browser console → network tab → WS message → session.trades in payload?

### "Wrong engine / regime on dashboard"
- ENGINE wrong: `runtime_config.json` freshness — `cat /opt/option_trading/.run/strategy_app/runtime_config.json`
- REGIME wrong: `session.signals[-1].regime` from WS snapshot — signals have regime per bar

### "Why did this trade fire?"
```python
# On VM:
python3 -c "
import json
from pathlib import Path
lines = Path('/opt/option_trading/.run/strategy_app/decision_traces.jsonl').read_text().splitlines()
for l in lines:
    d = json.loads(l)
    if d.get('final_outcome') == 'entry_taken' and 'HHMM' in str(d.get('timestamp','')):
        print(json.dumps(d, indent=2, default=str))
        break
"
```
Replace `HHMM` with the snapshot time, e.g. `10:33`.

### "Why was this trade blocked?"
Same script but change `entry_taken` to the specific snapshot_id. Look for:
- `primary_blocker_gate` at trace top level
- First gate with `status: blocked` in `candidates[0].ordered_gates`
- After 2026-06-01: `direction_consensus` blocked means `contra_regime` or `unclear_margin`

---

## Deploy Workflow

```bash
# Local development change
git add strategy_app/engines/direction_consensus.py
git commit -m "feat(direction): contra-regime veto for BREAKOUT"
git push origin mordenization

# Deploy to VM
gcloud compute scp strategy_app/engines/direction_consensus.py \
  option-trading-runtime-01:/tmp/direction_consensus.py --zone=asia-south1-b

gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --quiet --command="
sudo docker cp /tmp/direction_consensus.py \
  option_trading-strategy_app-1:/app/strategy_app/engines/direction_consensus.py &&
sudo docker exec option_trading-strategy_app-1 find /app -name '*.pyc' -delete &&
sudo docker restart option_trading-strategy_app-1 &&
echo 'deployed'
"
```

**Note:** For dashboard files (`.jsx`, `.py`), also restart `option_trading-dashboard-1`.

---

## Things That Will Confuse You

1. **`atm_options` in snapshot is empty.** Option prices are in `snapshot.strikes[ATM_STRIKE]`, not `snapshot.atm_options`. This is a legacy field that was replaced.

2. **`strategy_votes` MongoDB has `payload.vote` not `payload.signal`.** The dashboard signal inspector reads `payload.signal` (legacy schema). The votes collection was updated but the dashboard reader wasn't. See open issue E6.

3. **`execution_path: MISSING` on old traces.** Traces before 2026-06-01 12:05 don't have this field. It was added that day. Not a bug in old traces.

4. **`policy_checks: blocked` on old `entry_taken` traces.** Also pre-2026-06-01. The annotation was never mirrored to `ml_vote`. Fixed. New traces show `pass`.

5. **The dashboard URL has `mode=replay` even in LIVE tab.** The URL is the previous page state. The WATCHING dropdown state (`watchMode` in JS) controls what data is shown, not the URL. In LIVE mode, `watchMode=live` and the WS sends `mode:live`.

6. **`engine: deterministic` but trade inspector shows 0.5 for most metrics.** This is because `selectedVoteRaw` (from MongoDB) doesn't have ML probabilities for deterministic trades. The metrics shown are placeholders. The actual entry_prob is in `selectedVoteRaw.entry_prob` when the entryContext is populated.

---

## Glossary

| Term | Meaning |
|---|---|
| `consensus_bypass` | Trade fired via ML entry probability, bypassing formal policy gate chain |
| `shadow_score` | Composite signal score from 7+ market signals; negative = PE direction, positive = CE direction |
| `MFE` | Maximum Favorable Excursion — furthest the trade moved in our favour during hold |
| `MAE` | Maximum Adverse Excursion — worst point during hold |
| `TIME_STOP` | Exit because max_hold_bars reached (no profit target hit) |
| `contra_regime` | Direction consensus veto when trade direction contradicts regime classification |
| `BREAKOUT_BEAR` | Regime: market broke below opening range low with strong volume and aligned returns |
| `entry_prob` | ML model's confidence in entry (0.0–1.0); bypass threshold 0.65 |
| `direction_margin` | Difference between CE and PE direction scores; below threshold → direction vetoed |
| `size_multiplier` | Position sizing factor (0.25 = paper trading at 25% of full size) |
| `run_id` | Unique identifier for one live session; scopes all events to that session |

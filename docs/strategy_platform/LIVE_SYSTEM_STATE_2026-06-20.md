# Live System State — 2026-06-20

> Written after deploying `feat/compression-state-engine` to `option-trading-runtime-01`
> and running a full SIM verification. Use this as the reference for "what is deployed
> and why".

---

## Current Deployment

### Branch on runtime VM
```
option-trading-runtime-01:/opt/option_trading  →  feat/compression-state-engine
Latest commit: 37c92e7  fix(tests): 5 test isolation fixes
```

### Docker image state
The container image (`strategy_app`) was built from an older commit. Four application
files were **docker-cp'd** directly into the running container after deployment:

| File | What changed |
|---|---|
| `strategy_app/engines/deterministic_rule_engine.py` | `set_run_context` now resets policy on new `run_id` even with empty metadata (`_has_payload or new_run_id`) — fixes stale-policy bug on session restart |
| `strategy_app/engines/opportunity.py` | Opportunity gate scoring fixes |
| `strategy_app/engines/strategies/ml_entry.py` | Direction dual-bundle + compression model wiring |
| `strategy_app/ml/bundle_inference.py` | Non-NaN-wins merge for velocity features; xgboost version mismatch warning |

**CRITICAL**: If `strategy_app` container is **recreated** (not restarted), these files revert
to the stale image. You must re-cp them:
```bash
cd /opt/option_trading
sudo docker cp strategy_app/engines/deterministic_rule_engine.py option_trading-strategy_app-1:/app/strategy_app/engines/deterministic_rule_engine.py
sudo docker cp strategy_app/engines/opportunity.py                option_trading-strategy_app-1:/app/strategy_app/engines/opportunity.py
sudo docker cp strategy_app/engines/strategies/ml_entry.py        option_trading-strategy_app-1:/app/strategy_app/engines/strategies/ml_entry.py
sudo docker cp strategy_app/ml/bundle_inference.py                option_trading-strategy_app-1:/app/strategy_app/ml/bundle_inference.py
sudo docker restart option_trading-strategy_app-1
```

**To permanently fix**: rebuild the docker image from the compression branch.

---

## Container Status (as of 2026-06-20 17:41 IST)

| Container | Status | Key info |
|---|---|---|
| `strategy_app` | Healthy (restarted today) | compression branch code active |
| `snapshot_app` | Healthy | continuous market capture |
| `ingestion_app` | Healthy | Kite feed |
| `execution_app` | Healthy | Dhan adapter, paper mode |
| `dashboard` | Healthy | UI + SIM orchestration |
| `sim_orchestrator` | Healthy | Redis pub/sub, spawns replay containers |
| `seller_app` | Healthy | paper mode, SELLER_LIVE_ENABLED=0 |
| `redis` | Healthy 6 days | pub/sub + streams |
| `mongo` | Healthy 6 days | persistence |

---

## Key Configuration (from container env)

Verify at any time:
```bash
sudo docker exec option_trading-strategy_app-1 python -c "
import os
for k in ['ENTRY_ML_MODEL_PATH','ENTRY_ML_MIN_PROB','EXIT_STRATEGY_MODE',
          'ADAPTIVE_LOTTERY_REGIMES','REGIME_ALLOWED','STRATEGY_ROLLOUT_STAGE',
          'EXECUTION_ADAPTER','REGIME_DIRECTION_SIGNAL']:
    print(k, '=', os.getenv(k,'<not set>'))
"
```

Current values:
```
ENTRY_ML_MODEL_PATH        = /app/ml_pipeline_2/artifacts/entry_only/published/entry_compression_v1.joblib
ENTRY_ML_MIN_PROB          = 0.35
ML_ENTRY_DIRECTION_MODE    = multi_signal   ← stateless 6-signal scorer; abstains if |score|<2.0
ENTRY_MULTI_SIGNAL_MIN     = 2.0            ← abstain threshold (NOT SET = uses default 2.0)
EXIT_STRATEGY_MODE         = adaptive
ADAPTIVE_LOTTERY_REGIMES   = TREND,TRENDING,BREAKOUT  ← lottery on these; SIDEWAYS→scalper
LOTTERY_HARD_STOP_PCT      = 0.20
EXIT_SCALPER_HARD_STOP_PCT = 0.07
REGIME_ALLOWED             = None  (router maps CHOP/AVOID/PANIC→[] anyway)
STRATEGY_ROLLOUT_STAGE     = paper
EXECUTION_ADAPTER          = dhan
REGIME_DIRECTION_SIGNAL    = weighted  ← only used by regime_dual mode (not active in multi_signal)
```

### Direction flow (live, not what old docs said)
Entry-first, then direction confirmation — correct design:
```
ML prob ≥ 0.35 (entry gate passes)
  ↓
multi_signal scorer (reads current snapshot only, no rolling state):
  ORB break       ±2.0    VWAP side    ±2.0    straddle dominance ±2.0
  PCR change      ±1.0    VIX intraday ±1.5    EMA order          ±1.0
  ↓
|score| < 2.0 → ABSTAIN → no trade (direction too weak = coin-flip zone)
score ≥ 2.0   → CE
score ≤ -2.0  → PE
```

### Exit assignment by regime
```
Entry in TREND / TRENDING / BREAKOUT  →  LOTTERY exit
  (hold to thesis-fail or EOD, 20% hard stop)
Entry in SIDEWAYS / HIGH_VOL / other  →  SCALPER exit
  (3% target, 7% hard stop, thesis-fail 5 bars)
```
June 2 SIM entry was SIDEWAYS → **SCALPER** → 3% target hit in 1 bar. ✓ Correct.

---

## Why Fewer Trades Than the Old System

The old SIM containers (running `velocity_base_entry_bundle`, threshold=0.049, mode=`regime_dual`)
showed 6-10 entries/day. The current compression branch shows 0-1 entries/day. This is **correct**.

**What changed (all three factors combined make the system more selective):**

| Factor | Old config | New config | Impact |
|---|---|---|---|
| Model | `velocity_base_entry_bundle` | `entry_compression_v1` | Fires on compression setups only (specific market structure) |
| Threshold | **0.049** (≈ near-zero) | **0.35** (validated) | Old threshold = always-on noise |
| Direction | `regime_dual` | `multi_signal` with abstain | Abstains on weak signals (|score| < 2.0) |

**Isolation test (June 2, full day):**

| Config | Entries |
|---|---|
| Old: velocity_model, thresh=0.049, regime_dual | 6 |
| New model, thresh=0.05, regime_dual | 1 |
| New model, thresh=0.05, multi_signal | **2** |
| New model, thresh=0.35, regime_dual | 1 |
| New model, thresh=0.35, multi_signal (live) | 1 |

The model change (velocity → compression) is the dominant factor — drops 6→1 even at the same near-zero threshold. The threshold and direction mode are secondary gates.

**Gate breakdown on a typical day (June 2, 377 bars, threshold=0.05):**

| Gate | Bars blocked | What happens |
|---|---|---|
| Regime CHOP/AVOID | 123 | Router returns `[]`, no strategies run |
| Direction abstain (|score|<2.0) | 78 | ML fired, direction score too weak |
| Direction mismatch | 67 | ML voted one side, direction said other |
| Time window / cooldowns | ~25 | Outside session or reentry gap |
| **Entries taken** | **2** | Passed all gates |

**Zero trades on Jun 10-19 = correct.** The compression model looks for IV contraction + OI buildup (compression setup). That condition did not exist during those 10 days. The model should not fire; forcing trades would be buying noise.

---

## ML Model

- **Model**: `entry_compression_v1.joblib`
- **Location**: confirmed present in container at `/app/ml_pipeline_2/artifacts/entry_only/published/`
- **Features**: 40 (compression_score, adx_14, bb_width, range, EMA spreads, OI/PCR, velocity)
- **Threshold**: 0.35 (fires ~3% of bars on days with compression setup)
- **AUC**: 0.83 (2024 OOS holdout)

### Feature NaN behavior
- **First 14 bars** of each day: `adx_14`, `bb_width_20`, `range_10/30` etc. are NaN
  (rolling windows warming up). Model fills with training medians. Expected.
- **Historical snapshots (pre-2026-06-20)**: `velocity_enrichment.adx_14` and
  `velocity_enrichment.vol_spike_ratio` may be null because snapshot_app was on the
  old branch. From Monday 2026-06-23 onwards, live snapshots will have these populated.
- `max_nan_features` is not configured for ML_ENTRY (unlike `ml_pure` which has
  `ml_pure_max_nan_features=3`). Inference proceeds even with NaN features (median fill).

---

## SIM Verification — 2026-06-20

Ran SIM on June 19 (last trading day) via `POST /api/sim/runs` on dashboard port 8008.

### Results
- **Bars replayed**: 374 (full day 09:15–15:15 IST)
- **Entries taken**: 0
- **Exits**: 0
- **Decision traces**: 374

### Why zero entries (correct behavior)
| Gate | Bars blocked | Reason |
|---|---|---|
| `no_strategy_votes` | 346 | Regime = CHOP (vol_ratio 0.56-0.73, returns_mixed) → routes to `[]` |
| `avoid_veto` (IV_FILTER) | 23 | TRENDING_BEAR bars, but IV percentile 97-100% > 95% threshold |
| `entry_time_windows` | 5 | Outside 09:35-15:00 IST window |

June 19 was a structurally choppy low-volatility day. The system correctly identified
this and abstained. Zero entries = **correct**.

### ML_ENTRY on TRENDING bars
The 8 TRENDING_BEAR bars that reached ML_ENTRY — model returned prob < 0.35.
On a CHOP/low-vol day, the compression setup was not present → low model score → no vote.
This is the model working as designed (selective, not omnivorous).

---

## Regime Router Logic

```
TRENDING / SIDEWAYS / HIGH_VOL / PRE_EXPIRY / EXPIRY / BREAKOUT → ['IV_FILTER', 'ML_ENTRY']
AVOID / CHOP / PANIC / DEAD_MARKET                               → []  (no trades)
EXIT                                                             → [exit strategy list]
```

Entry requires ALL of:
1. Regime not CHOP/AVOID/PANIC
2. IV_FILTER: iv_percentile ≤ 95% (blocks extreme IV days)
3. ML_ENTRY: entry_compression_v1 prob ≥ 0.35
4. Within time window 09:35–15:00 IST
5. Consensus gate: at least 1 ENTRY vote (if IV_FILTER votes SKIP, no entry)

---

## Known Issues / Monitoring Points

1. **Image staleness**: Container image is not up to date. Docker-cp approach is fragile —
   rebuild image when stable (or before next major deploy).

2. **Compression features in old snapshots**: SIM on pre-June-20 dates will show NaN
   for `adx_14`, `vol_spike_ratio` from `velocity_enrichment`. SIM still runs; model
   uses median fill. For accurate SIM of compression behavior, use June 23+ dates.

3. **Futures rollover**: `BANKNIFTY26JUNFUT` expires June 26. Update
   `BANKNIFTY_FUTURES_SYMBOL=BANKNIFTY26JULFUT` on/before June 23.

4. **Capital**: Balance ~₹24k. ATM monthly premium ~₹33k/lot. Consider weekly expiry
   strikes or capital top-up before going live.

5. **Seller paper validation**: `seller_app` is in paper mode. Run 1-2 weeks paper
   before enabling live (`SELLER_LIVE_ENABLED=1`).

---

## How to Trigger a SIM

```bash
# On runtime VM or via SSH:
curl -s -X POST "http://localhost:8008/api/sim/runs" \
  -H "Content-Type: application/json" \
  -d '{"source_date":"2026-06-19","label":"verify","speed":100}'

# Get result (replace run_id):
curl -s "http://localhost:8008/api/sim/runs/b1239ab5-87ec-469b-83ea-59979d9100d3"
```

Or query MongoDB directly:
```js
// In mongosh:
db.strategy_decision_traces_sim.countDocuments({run_id:"<run_id>"})
db.trade_signals_sim.find({run_id:"<run_id>",signal_type:"ENTRY"})
```

---

## What to Watch on Monday (June 23)

1. **Morning: live snapshots from 09:15** — confirm `velocity_enrichment.adx_14` is non-null
   after bar 14 (09:29). Check via:
   ```bash
   sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --eval \
     'db.phase1_market_snapshots.findOne({trade_date_ist:"2026-06-23",market_time_ist:"09:40:00"}).payload.snapshot.velocity_enrichment.adx_14'
   ```

2. **Kite token**: expires daily. Refresh before 09:15.

3. **First compression entry** (if it fires): confirm `ml_entry_prob` is non-null in
   `strategy_votes` and direction is populated. If `ml_entry_prob=null` in votes,
   the model is not scoring (bundle load failure — check logs).

4. **IV regime**: If IV percentile < 95, IV_FILTER allows → ML_ENTRY can vote.
   June was high-IV. If July reverts to normal IV, entries will start firing.

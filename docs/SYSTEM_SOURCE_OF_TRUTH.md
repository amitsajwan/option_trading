# BankNifty System — Source of Truth

As-of date: **2026-06-14** (updated end-of-day)

> If active docs conflict with code, code wins. If active docs conflict with each
> other, this file wins.

---

## 1. How the Pipeline Works — What Goes Where and When

Every 1-minute bar flows through the system in this exact order:

```
DATA LAYER (snapshot_app)
  Kite live feed → snapshot_app → computes per-bar:
    futures_bar         (OHLCV of BankNifty futures)
    mtf_derived         (atr_14_1m, ema_9/21, bb_pct_b_5m, ema_trend_5m/15m, mtf_aligned)
    chain_aggregates    (atm_strike, max_pain, PCR, atm_straddle_price)
    atm_options         (atm_ce_close, atm_pe_close, OI, OI change 1m/30m, IV)
    strikes             (25-entry list: each has strike, ce_ltp, pe_ltp, ce_oi, pe_oi)
    session_context     (time HH:MM:SS, vwap, opening_range, trade_date_ist)
  → stored in MongoDB (trading_ai.phase1_market_snapshots) + Redis pub/sub

STRATEGY ENGINE (strategy_app) — runs each bar:
  GATE 1  TIME        → only within ENTRY_TIME_WINDOWS (09:45–14:30 IST)
  GATE 2  VOL         → ENTRY_VOL_GATE_ENABLED=1: atr_14_1m / fut_close >= 0.00088
                         (uses mtf_derived.atr_14_1m — level-invariant, not absolute)
                         ENTRY_VOL_GATE_ENABLED=0: entry_only_v3 model prob >= ENTRY_ML_MIN_PROB
  GATE 3  REGIME      → profile=trader_master_live_v1 maps Regime enum to strategy list
                         CHOP → [] (no strategies → no trades)
                         AVOID/PANIC/DEAD_MARKET → [] (no trades)
                         TRENDING/SIDEWAYS/BREAKOUT/HIGH_VOL → ['IV_FILTER','VOL_GATE_ENTRY']
                         Note: ENTRY_REGIME_ALLOWED_TAGS is disabled (empty) — regime filter is profile-level
  GATE 4  DIRECTION   → ML_ENTRY_DIRECTION_MODE=regime_dual (40% ML + 60% composite heuristic)
                         heuristic: combo of momentum_5m, vwap, momentum_15m, iv_skew
                         Returns CE, PE. ABSTAIN possible if no agreement.
  GATE 5  ENTRY       → buy OTM CE or PE via SMART_STRIKE (premium ₹600–₹1300)
                         Records the SPECIFIC ENTRY STRIKE (not the rolling ATM)
                         June 12 actual: 56500 CE at ₹769–781 (ATM was ~₹1117)

POSITION MANAGEMENT (exits checked every bar, ADAPTIVE routing by Regime enum):
  TRENDING/BREAKOUT → LOTTERY stack:
    - Thesis fail: DISABLED (LOTTERY_THESIS_FAIL_BARS=999, LOTTERY_THESIS_FAIL_MIN_MFE=0.03)
    - Hard SL: 20% (LOTTERY_HARD_STOP_PCT=0.20) but universal 10% floor fires first
    - Big TP: 50% (LOTTERY_BIG_TARGET_PCT=0.50)
    - Runner trail: activates at +20% MFE (LOTTERY_RUNNER_ACTIVATION_MFE=0.20), gives back 35% from peak
    - Timestop: 90 bars (LOTTERY_TIMESTOP_BARS=90)
    - Momentum flip exit: LOTTERY_MOMENTUM_FLIP=1.0
  Others (SIDEWAYS, HIGH_VOL) → SCALPER stack:
    - Thesis fail: DISABLED (EXIT_THESIS_FAIL_BARS=999)
    - Hard SL: 7% (EXIT_SCALPER_HARD_STOP_PCT)
    - TP: 3% (EXIT_PREMIUM_TARGET_PCT)
    - Trail: activates at +1.5%, trails 0.8% from peak
  Universal outer: 10% SL wraps everything (EXIT_MAX_LOSS_PCT=0.10)
  EOD: soft close at end of time window or 15:05 IST
```

**Key data fact**: the `strikes` list in every snapshot has 25 entries with `ce_ltp` / `pe_ltp` for each strike. At exit, look up the SPECIFIC ENTRY STRIKE here — not `atm_ce_close` (which is the current rolling ATM, a different strike after any market move).

---

## 2. Runtime Config — Current (verified 2026-06-14 from live API)

**VM:** `option-trading-runtime-01`, zone `asia-south1-b`, project `amit-trading`  
**Config file (only one):** `/opt/option_trading/.env.compose`  
**Old `algo-trading-496203` and `amittrading-493606` projects are DEAD — never reference them.**

> **Being consolidated** → all strategy tunables below are moving into a single
> grouped YAML (`ops/strategy_config.yml`) read by both live and sim, so SIM can
> no longer diverge from LIVE. Plan + phases:
> [`docs/strategy_platform/CONFIG_CONSOLIDATION_PLAN.md`](strategy_platform/CONFIG_CONSOLIDATION_PLAN.md).
> Until that lands, `.env.compose` remains the single live config file.

```bash
# Execution
EXECUTION_ADAPTER=dhan               # ACTUAL .env.compose value (parity-verified 2026-06-14).
                                     # Doc intent was "paper until validated" — this is DRIFT.
                                     # BUT no real fills occur: execution_app is DOWN, rollout_stage=paper,
                                     # tier gate requires tier==live, and June orders were IP-rejected.
                                     # SIM always forces EXECUTION_ADAPTER=paper. See execution forensic.
rollout_stage=paper
strategy_profile_id=trader_master_live_v1

# Entry — ATR vol gate (ML disabled)
ENTRY_VOL_GATE_ENABLED=1             # 1 = ATR gate (no ML); 0 = entry_only_v3 model
ATR_ENTRY_MIN_PCT=0.00088            # p90 of live ATR — ~9% of bars pass
ENTRY_ML_MIN_PROB=0.45               # only used if ENTRY_VOL_GATE_ENABLED=0
ENTRY_TIME_WINDOWS=09:45-14:30       # IST entry window (NOT 09:35-15:05)
ENTRY_REGIME_ALLOWED_TAGS=           # DISABLED — regime filter is done by profile: CHOP→[] (no strategies)

# Strike selection — OTM, NOT ATM
STRATEGY_STRIKE_SELECTION_POLICY=otm # buys OTM strike in premium range below
SMART_STRIKE_MIN_PREMIUM=0           # code default — NO floor (prior doc said 600; it was never set).
                                     # MAX cap 1300 is the real constraint.
SMART_STRIKE_MAX_PREMIUM=1300        # don't buy more expensive than ₹1300
STRATEGY_STRIKE_MAX_OTM_STEPS=12     # max 12 steps OTM (premium floor enforces practical limit)
# Result: June 12 entries were 56500 CE at ₹769-781, NOT ATM ₹1117

# Direction
ML_ENTRY_DIRECTION_MODE=regime_dual  # uses direction ML model (not combo/weighted)
DIRECTION_ML_WEIGHT=0.40             # 40% ML weight, 60% heuristic composite

# Exit — ADAPTIVE (routes by Regime enum, NOT RegimeDirector quality)
EXIT_STRATEGY_MODE=adaptive           # TRENDING/BREAKOUT → lottery; others → scalper
EXIT_MAX_LOSS_PCT=0.10               # UNIVERSAL FLOOR: 10% of entry price, wraps entire stack
# Scalper stack (SIDEWAYS, HIGH_VOL, CHOP would be here but CHOP gets no entries)
EXIT_SCALPER_HARD_STOP_PCT=0.07      # 7% of entry (for ₹769 entry: ₹54 SL)
EXIT_PREMIUM_TARGET_PCT=0.03         # 3% TP (for ₹769 entry: ₹23 TP) — tight R:R
EXIT_TRAILING_ACTIVATION_PCT=0.015   # trail activates at +1.5% gain
EXIT_TRAILING_TRAIL_PCT=0.008        # trail 0.8% from peak
EXIT_THESIS_FAIL_BARS=999            # DISABLED (in scalper path) — no early cut for dead trades
# Lottery stack (BREAKOUT, TRENDING)
ADAPTIVE_LOTTERY_REGIMES=BREAKOUT,TRENDING
LOTTERY_HARD_STOP_PCT=0.20           # 20% nominal, but universal 10% floor fires first
LOTTERY_BIG_TARGET_PCT=0.50          # 50% TP (for ₹769 entry: ₹385 gain)
LOTTERY_RUNNER_ACTIVATION_MFE=0.20   # trail activates once MFE ≥ 20%
LOTTERY_RUNNER_GIVEBACK_FRAC=0.35    # give back 35% from peak before locking
LOTTERY_THESIS_FAIL_BARS=999         # DISABLED — hold lottery until TP/SL/timestop
LOTTERY_THESIS_FAIL_MIN_MFE=0.03     # (only fires if LOTTERY_THESIS_FAIL_BARS < 999)
LOTTERY_TIMESTOP_BARS=90             # max 90-bar (90 min) hold in lottery
LOTTERY_MOMENTUM_FLIP=1.0            # exit on momentum reversal

# Risk
RISK_MAX_CONSECUTIVE_LOSSES=6
RISK_MAX_SESSION_TRADES=6
RISK_MAX_LOTS_PER_TRADE=1            # 1 lot always (real-money safety cap)
RISK_CAPITAL_ALLOCATED=41000        # live Dhan balance (sizing base)
RISK_PER_TRADE_PCT=0.005
```

> **Authoritative source for these values is now `ops/strategy_config.yml`**
> (parity-verified against `.env.compose`). This block is a human-readable mirror;
> if it ever disagrees, the YAML + `python ops/config_parity.py` win. The exact
> per-bar gate order is in [`strategy_platform/REAL_ALGO_STEPWISE.md`](strategy_platform/REAL_ALGO_STEPWISE.md).
> The PROPOSED redesign (opportunity gate = selection, direction = straddle-default)
> is separate and NOT live — see `OPPORTUNITY_GATE_DESIGN.md`,
> `GATE_FORENSICS_AND_CONFIG.md`, `DIRECTION_TREE_FINDINGS.md`.

**Exit numerics for June 12 OTM entry (₹769 entry):**
| Path | When | Hard SL | TP | ThesisFail | Timestop |
|------|------|---------|-----|------------|---------|
| Universal floor | Always (outer) | -10% = -₹77 | — | — | — |
| Scalper | SIDEWAYS, HIGH_VOL | -7% = -₹54 | +3% = +₹23 | DISABLED (999) | — |
| Lottery | TRENDING, BREAKOUT | -10% floor (20% nominal never reached) | +50% = +₹385 | DISABLED (999) | 90 bars |
| CHOP | No entries from profile | — | — | — | — |

**How to verify running config (source of truth):**
```bash
curl http://<vm-ip>:8008/api/ops/config   # best: reads ops_env.json written by strategy_app
gcloud compute ssh option-trading-runtime-01 --zone asia-south1-b --project amit-trading \
  -- sudo docker exec option_trading-strategy_app-1 printenv | grep -E "ENTRY|REGIME|EXIT|LOTTERY"
```

---

## 3. Active ML Model

| Asset | Detail |
|---|---|
| Bundle | `entry_only_v3` (020pct label, ≥110pt move in 5 min) |
| AUC | 0.831, ECE 0.009 (calibrated) |
| Threshold | 0.45 (fire_rate 0.79%, precision 59%) |
| GCS | `gs://amit-trading-option-trading-models/published_models/entry_only_v3/` |
| Container path | `/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model_020pct.joblib` |
| xgboost | **must be 3.2.0** — mismatch produces degenerate output (all probs ~0.826) |

**When ENTRY_VOL_GATE_ENABLED=1 (current config), the ML model is NOT called at all.**  
ATR gate runs instead. Only enable ML gate after confirming xgboost version matches.

---

## 4. Strategy Verdicts (what we know)

| Component | Verdict | Basis |
|---|---|---|
| Entry (magnitude) | ✅ SOLVED | AUC 0.831, 4.37× lift on big moves |
| Direction (buyer) | ⚠️ UNRESOLVED | Simulation was broken; 2024 grid definitive result pending |
| S3 Seller | ✅ ONLY PROVEN PATH | 78% win, +₹1,692/trade, 2024 paper |
| Real money | ❌ OFF | Seller needs live-cycle paper; buyer unresolved |
| **June 12 live** | **+32.5% (trade 2), lottery held winner** | Actual: 56500 CE ₹781→₹1034 in 64 bars |

**Direction context**: previous "dead" verdict (50.3% coin flip) was measured from correct
underlying-direction accuracy data (37,050 move-bars). That measurement is still valid.  
However, *two simulation bugs* masked the true P&L picture — see Section 5.  
The 2024 grid with fixed simulation will give the definitive P&L verdict.

---

## 5. Simulation Bugs Found 2026-06-14 — Now Fixed

Both bugs caused every simulation prior to this date to show artificially pessimistic P&L for buyers.

**Bug 1 — Rolling ATM exit price (masked all gains)**
- WRONG: use `atm_ce_close` at exit = price of the CURRENT ATM strike
- WHY WRONG: when BANKNIFTY moves +300pt, the ATM rolls to a new strike.
  The new ATM CE is worth ~same ₹200. But the HELD 53500-CE (now deep ITM) is worth ₹500+.
  Old sim reported ₹0 gain; reality was ₹300 gain. Big wins showed up as flat.
- FIXED: look up the SPECIFIC ENTRY STRIKE in `inner["strikes"]` list at exit.
  `_get_strike_ltp(inner, entry_strike, side)` — search 25-entry chain for exact strike.

**Bug 2 — Fixed N-bar hold (gave back all gains)**
- WRONG: exit after exactly N bars regardless of what happened
- WHY WRONG: the option peaked (MFE avg +22–44pt above entry), then reversed.
  Fixed hold always exited after the reversal, showing losses.
- FIXED: exit when TP or SL hit (checked each bar during hold), fall back to N-bar max.

**With both bugs fixed (2026 grid, 10 days, 135 cells × 4 TP/SL configs):**
- All configs with vol=off or vol=atr_low are still negative
- `vol=atr_high + regime=trend_only + dir=weighted + TP=50pt + SL=30pt` → +8.07pt avg, 64% WR, **14 trades**
- 14 trades = statistically meaningless. 2024 data (hundreds of trades) needed to validate.

**Correct simulation exit logic:**
```python
# Entry: record specific strike
e_strike = chain_aggregates["atm_strike"]
e_price  = atm_ce_close  # or atm_pe_close

# Each bar during hold:
cur_px = get_strike_ltp(inner, e_strike, side)  # look up SPECIFIC strike, not rolling ATM
gain   = cur_px - e_price
if gain >= TP:       # exit at profit target
if gain <= -SL:      # exit at stop loss
if bars >= max_hold: # exit at max duration
```

---

## 6. GCP Operations

```bash
# SSH
gcloud compute ssh option-trading-runtime-01 --zone asia-south1-b --project amit-trading

# Check containers (13 total when healthy)
sudo docker ps

# PREFERRED: check live config from ops API (reads strategy_app's actual env, not your shell env)
curl http://localhost:8008/api/ops/config

# Backup: check env vars directly in container
sudo docker exec option_trading-strategy_app-1 printenv | grep -E "ENTRY|REGIME|EXIT|LOTTERY"

# Run OPS SIM for a specific date (replays that day through strategy_app_historical)
curl -X POST http://localhost:8008/api/ops/sim/today \
     -H "Content-Type: application/json" \
     -d '{"date":"2026-06-12","overrides":{}}'
# Returns {job_id, actual_trade_count}. Poll: GET /api/ops/sim/{job_id}

# Restart with updated env (must use --env-file, NOT docker restart)
cd /opt/option_trading
sudo docker compose --env-file .env.compose up -d

# Start ML VM (stopped when idle)
gcloud compute instances start option-trading-ml-01 --zone asia-south1-b --project amit-trading
```

---

## 7. OPS SIM Fidelity — Compose Sync (2026-06-14)

**Problem fixed 2026-06-14**: `strategy_app_historical` env block was missing ~50 strategy
vars (all exit/lottery/entry-quality/risk vars). They defaulted to wrong code defaults in SIM
but came from `.env.compose` in live → SIM was running different config.

**Fix**: all missing vars added to compose `strategy_app_historical` using `${VAR_HISTORICAL:-${VAR:-default}}`
pattern. Now historical inherits live config from `.env.compose` by default.
To A/B test a param: set `VAR_HISTORICAL=different_value` in `.env.compose`.

**Remaining gap**: regime classification diverges. `vol_ratio` in `futures_derived` is a
point-in-time snapshot; the live system may have computed it slightly differently at execution.
June 12: 368/375 bars → CHOP in SIM (weak-vol + mixed returns), but live entered 2 trades.
Both entry bars were in a brief window where live vol_ratio crossed the threshold; the stored
snapshot has a slightly different value. This is a fundamental replay-fidelity limitation.
Use `actual_trades[]` (real fills from positions.jsonl) for P&L; SIM replay is for override A/B testing.

**SIM output:**
- `actual_trades` — real closed trades from that day ✅ (always accurate)
- `trades[]` — sim-replayed trades (may still miss warm-up entries) ⚠️

## 8. Grid Experiments — ops/grid.yml + ops/run_grid.py

**Design**: `ops/grid.yml` declares named cells with env-var override bundles.
`ops/run_grid.py` submits one SIM per cell × date and prints a P&L table.

```bash
# Run full grid
python ops/run_grid.py --vm-ip <VM_IP>

# Single cell
python ops/run_grid.py --vm-ip <VM_IP> --cell scalper_mode

# Dry run (print plan only)
python ops/run_grid.py --dry-run

# Override dates inline
python ops/run_grid.py --vm-ip <VM_IP> --dates 2026-06-12 2026-06-11
```

**Pattern for A/B testing a single param** (no grid.yml edit needed):
```bash
# From any machine with network access to VM:
curl -X POST http://<VM_IP>:8008/api/ops/sim/today \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-06-12","overrides":{"EXIT_STRATEGY_MODE":"scalper","EXIT_SCALPER_HARD_STOP_PCT":"0.05"}}'
```

Safe override keys are defined in `market_data_dashboard/routes/ops_routes.py:_SAFE_OVERRIDE_KEYS`.

---

## 9. Reference Docs

| Doc | What it answers |
|---|---|
| `docs/FINDINGS_2026-06-14.md` | Full evidence base — all verdicts with data |
| `docs/TWO_REGIME_SYSTEMS.md` | Regime enum vs RegimeDirector quality (naming confusion) |
| `docs/CONFIG_SAFE_OPS.md` | How to change code/config safely without losing fixes |
| `docs/RUNTIME_STATE_AND_RECOVERY.md` | VM rebuild + config snapshot |
| `docs/strategy_platform/05_CONFIG_REFERENCE.md` | Every env var with default and meaning |
| `docs/FINDINGS_2026-06-14.md` | Full evidence base — all verdicts with data |

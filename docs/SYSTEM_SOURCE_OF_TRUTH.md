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
  GATE 1  TIME        → only within ENTRY_TIME_WINDOWS (09:35–15:05 IST)
  GATE 2  VOL         → ENTRY_VOL_GATE_ENABLED=1: atr_14_1m / fut_close >= 0.00088
                         (uses mtf_derived.atr_14_1m — level-invariant, not absolute)
                         ENTRY_VOL_GATE_ENABLED=0: entry_only_v3 model prob >= ENTRY_ML_MIN_PROB
  GATE 3  REGIME      → regime_quality(snap) must be TREND or MID
                         (from mtf_derived: ema_trend_5m/15m alignment + bb_pct_b_5m position)
                         CHOP = no trade
  GATE 4  DIRECTION   → RegimeDirector(signal=combo).decide(snap)
                         combo = [mom15 + atm_oi + max_pain all agree] AND [ema_9 vs ema_21 agree]
                         Returns CE, PE, or ABSTAIN. ABSTAIN = no trade.
  GATE 5  ENTRY       → buy ATM CE or PE at current atm_ce_close / atm_pe_close
                         Record the SPECIFIC ATM STRIKE (chain_aggregates.atm_strike)

POSITION MANAGEMENT (exits checked every bar):
  EXIT A  HARD STOP   → EXIT_SCALPER_HARD_STOP_PCT=0.05
                         = 5% of the entry option price in rupees
                         Checked against the SPECIFIC HELD STRIKE price (from strikes list)
  EXIT B  THESIS FAIL → regime flips to CHOP → REGIME_SHIFT exit
  EXIT C  EOD         → forced close at 15:05 IST
```

**Key data fact**: the `strikes` list in every snapshot has 25 entries with `ce_ltp` / `pe_ltp` for each strike. At exit, look up the SPECIFIC ENTRY STRIKE here — not `atm_ce_close` (which is the current rolling ATM, a different strike after any market move).

---

## 2. Runtime Config — Current (as deployed 2026-06-14)

**VM:** `option-trading-runtime-01`, zone `asia-south1-b`, project `amit-trading`  
**Config file (only one):** `/opt/option_trading/.env.compose` (185-line brain config)  
**Old `algo-trading-496203` and `amittrading-493606` projects are DEAD — never reference them.**

```bash
# Execution
EXECUTION_ADAPTER=paper              # NEVER 'dhan' until live-cycle paper passes

# Entry vol gate
ENTRY_VOL_GATE_ENABLED=1             # 1 = ATR gate (no ML); 0 = entry_only_v3 model
ATR_ENTRY_MIN_PCT=0.00088            # p90 of live ATR — 9% of bars pass
ENTRY_ML_MIN_PROB=0.45               # threshold if ENTRY_VOL_GATE_ENABLED=0
ENTRY_ML_MODEL_PATH=/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model_020pct.joblib

# Direction
REGIME_DIRECTION_SIGNAL=combo        # mom15 + atm_oi + max_pain + ema — fires rarely but higher quality
REGIME_W_MOM=1.0                     # (only matters if signal=weighted)
REGIME_W_MAXPAIN=0.8
REGIME_W_OI=0.8
REGIME_W_VWAP=1.0
REGIME_W_EMA=0.5

# Exit
EXIT_STRATEGY_MODE=scalper           # scalper uses EXIT_SCALPER_HARD_STOP_PCT
EXIT_SCALPER_HARD_STOP_PCT=0.05      # 5% of entry option price = hard stop in rupees

# Regime quality
ENTRY_REGIME_ALLOWED_TAGS=MID,TREND  # CHOP = no trade
```

**How to verify running config:**
```bash
gcloud compute ssh option-trading-runtime-01 --zone asia-south1-b --project amit-trading
sudo docker exec option_trading-strategy_app-1 printenv | grep -E "ENTRY|REGIME|EXIT"
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
| Direction (buyer) | ⚠️ UNRESOLVED | Simulation was broken; re-validating on 2024 with fixed sim |
| S3 Seller | ✅ ONLY PROVEN PATH | 78% win, +₹1,692/trade, 2024 paper |
| Real money | ❌ OFF | Seller needs live-cycle paper; buyer unresolved |

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

# Check containers
sudo docker ps

# Check running config
sudo docker exec option_trading-strategy_app-1 printenv | grep -E "ENTRY|REGIME|EXIT|EXECUTION"

# Restart with updated env (must use --env-file, NOT docker restart)
cd /opt/option_trading
sudo docker compose --env-file .env.compose up -d

# Start ML VM (stopped when idle)
gcloud compute instances start option-trading-ml-01 --zone asia-south1-b --project amit-trading
```

---

## 7. Reference Docs

| Doc | What it answers |
|---|---|
| `docs/FINDINGS_2026-06-14.md` | Full evidence base — all verdicts with data |
| `docs/TWO_REGIME_SYSTEMS.md` | Regime enum vs RegimeDirector quality (naming confusion) |
| `docs/CONFIG_SAFE_OPS.md` | How to change code/config safely without losing fixes |
| `docs/RUNTIME_STATE_AND_RECOVERY.md` | VM rebuild + config snapshot |
| `docs/strategy_platform/05_CONFIG_REFERENCE.md` | Every env var with default and meaning |

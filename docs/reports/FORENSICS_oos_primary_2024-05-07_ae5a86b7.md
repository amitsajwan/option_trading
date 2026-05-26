# Trade forensics report — OOS primary (May–Jul 2024)

**Run ID:** `ae5a86b7-9198-4e64-9399-fd5fea03e293`  
**Profile:** `trader_master_ml_entry_v1` + unified direction ML  
**Window:** 2024-05-01 → 2024-07-31  
**Emitted snapshots:** 23,412  
**Generated:** 2026-05-24 (VM `analyze_trade_forensics.py` + `analyze_oos_validation_run.py`)

---

## 1. Executive summary

| Metric | Value |
|--------|-------|
| Closed trades | **541** |
| Win rate | 46% |
| Portfolio PF | **1.00** |
| Total cap PnL | +0.2% |
| Avg / median opt PnL | +0.1% / −1.0% |

**Verdict:** Book is flat (PF≈1) with strong winners captured by trailing stops offset by **TIME_STOP losers** and **PE leg underperformance**. Primary levers: exits (stagnant/time stop), session trade cap (missed entries), direction quality, IV avoid_veto.

---

## 2. Coverage

| Month | Closes | Cap PnL % |
|-------|--------|-----------|
| 2024-05 | 174 | +5.3% |
| 2024-06 | 176 | +14.5% |
| 2024-07 | 191 | **−19.7%** |

Trade days: 2024-05-03 .. 2024-07-31 (59 days). All entries `ML_ENTRY`. Direction: PE 300, CE 241.

---

## 3. Legs

| Leg | n | WR | Avg PnL | Leg PF |
|-----|---|-----|---------|--------|
| CE | 241 | 53% | +1.7% | **1.36** |
| PE | 300 | 41% | −1.2% | **0.79** |

July drawdown drives portfolio fail (PF target 1.3).

---

## 4. Exit breakdown

| Exit | n | WR | Avg PnL | PF |
|------|---|-----|---------|-----|
| **TIME_STOP** | 339 | 35% | **−3.2%** | **0.24** |
| TRAILING_STOP | 129 | 97% | +17.4% | 75.1 |
| STOP_LOSS | 67 | 0% | −24.6% | 0.00 |
| TARGET_HIT | 6 | 100% | +86.1% | ∞ |

**TIME_STOP is the book killer** (63% of trades, negative expectancy).

---

## 5. Entry funnel

| Stage | Count |
|-------|-------|
| ML_ENTRY votes (all) | 215 |
| Votes prob ≥ 0.65 | 215 |
| Closed trades | 541 |
| Votes≥0.65 matched to trade | 117 (54.4%) |
| **Missed** (vote≥0.65, no trade) | **98** |

### Missed-entry blockers

| Blocker | Missed count |
|---------|----------------|
| session_trade_cap | 83 |
| avoid_veto (IV_FILTER) | 15 |

### Top decision-trace blockers (all blocked)

| Blocker | Count |
|---------|-------|
| session_trade_cap | 1,628 |
| entry_phase | 1,610 |
| avoid_veto | 1,107 |

---

## 6. Direction vs BN 5m (forward 5-bar excursion)

| Flag | Trades |
|------|--------|
| DIR_OK_VS_BN_5M | 285 |
| DIR_WRONG_VS_BN_5M | 243 |
| BETTER_OPP_SIDE | 161 |

~45% of trades flagged wrong vs 5m BN bias; 161 had materially better opposite side.

**Note:** `direction_source` not persisted on most votes (`unknown` for 541 trades) — limits direction-model forensics until vote payload is enriched.

---

## 7. Diagnosis flags (per trade)

| Flag | Count |
|------|-------|
| NO_ML_VOTE_MATCH | 424 |
| TIME_STOP_LOSER | 160 |
| TRAIL_OK | 125 |
| BAD_ENTRY_NO_RUN | 96 |
| LEFT_PROFIT_ON_TABLE | 52 |
| TIME_STOP_EARLY | 42 |
| STOP_HIT | 67 |

---

## 8. Layer verdict

| Layer | Assessment |
|-------|------------|
| **Entry** | 98 missed high-prob votes; session cap dominant |
| **Gates** | entry_phase + session_trade_cap + IV avoid_veto choke throughput |
| **Direction** | 404 trades flagged (wrong side / better opp) |
| **Exit** | 94 trades — profit left on table / early time stop; TIME_STOP PF 0.24 |

---

## 9. Recommended actions (priority)

1. **TIME_STOP / stagnant exit** — raise `stagnant_exit_bars` (12→20) or dyn_exit v2; cut loser stagnation before 12 bars at −3% avg.
2. **Session trade cap** — eval with `RISK_MAX_SESSION_TRADES=0` or 24 to measure uplift; production cap=6–12 still valid for live.
3. **PE leg** — investigate direction model / `ML_ENTRY_BLOCK_CE` vs improving PE calibration.
4. **IV_FILTER** — review `extreme_iv_percentile=95` for eval; 15 missed on avoid_veto.
5. **Dual direction** — argmax gate fix deployed (`9b639d8`); re-run E3-S6 after consumer-drain fix.
6. **Vote telemetry** — persist `direction_source` + `entry_prob` on ML_ENTRY votes for forensics join (424 NO_ML_VOTE_MATCH today).

---

## 10. New replay caveat (`91512ba5`)

Fresh replay (2026-05-24) emitted 23,412 snapshots but Mongo only had **44 closes (May 2–7)** when analyzed ~1 min after “completed”. Eval API marks done when **emission** finishes, not when the historical consumer drains the queue. Use this reference run for full-window analysis until `run_full_forensics_report.sh` waits for close-count stabilization.

---

*CSV (541 rows): VM `/opt/option_trading/.run/forensics_reports/trades_ae5a86b7.csv` (generate with `--csv` if not present).*

# Plan — R1S with max 3 trades per day

**Date:** 2026-05-20  
**Constraint:** At most **3 new entries per calendar day** — pick the **best** setups, not every minute the rule fires.  
**Base rule:** R1S / `T1_FADE_E0_BASELINE` (sell ATM CE, ORB-down fade).  
**Why:** Unfiltered R1S fires **~200–280 trades/quarter**; payoff asymmetry (1 stop ≈ 2 targets) means **quality > count**.

---

## 1. Hypothesis

| Today (unfiltered) | Proposed (top 3/day) |
|--------------------|----------------------|
| Every `signal==1` bar → trade after prior exit | Max **3 entries/day**, ranked by setup strength |
| High count, ~50–60% WR | Lower count, hope: **higher WR, better mean trade, better t-stat** |
| Stress quarters: many stops | Fewer shots in bad chop; may **not** fix Aug–Oct alone |

**We are not assuming top-3 fixes regime.** We test whether it improves **audit PASS count** vs baseline 6/17.

---

## 2. What “best 3” means (scoring options to test)

All candidates must already pass **R1S entry + disqualifiers**. Among those rows on the same `trade_date`, rank and keep **top 3**.

| Variant | Score (higher = take first) | Trader rationale |
|---------|----------------------------|------------------|
| **S0 — First come** | Earliest `minute` after 9:30 | Simple baseline; “first clean fade after OR” |
| **S1 — Momentum strength** | `abs(ret_5m)` | Strongest immediate down move → richest CE to sell |
| **S2 — VWAP stretch** | `abs(vwap_distance)` | Most extended below VWAP → best mean-revert potential |
| **S3 — Composite** | `0.5*abs(ret_5m) + 0.5*abs(vwap_distance)` | Balance momentum + stretch |
| **S4 — Calm stretch** | `abs(vwap_distance) / max(osc_atr_percentile, 0.1)` | Fade when stretched but not in explosion |

**Recommendation:** Run **S0, S1, S3** in wave A (3 scoring rules × 1 exit = 3 rule configs). Skip S2/S4 unless S1/S3 move the needle.

**Tie-break:** earlier `minute` wins.

---

## 3. Engine changes (implementation)

### 3.1 New module: `trade_selection.py`

```text
apply_daily_trade_cap(signals: Series, df: DataFrame, max_per_day: int, score_columns: list) -> Series
```

- Input: boolean `signal` from `generate_signals`
- Per `trade_date`: among rows with `signal==True`, compute score, sort desc, keep **top 3**, zero out rest
- Output: filtered `signal` series → `simulate_trades` unchanged

### 3.2 Rule JSON (optional fields)

```json
{
  "max_trades_per_day": 3,
  "trade_score": {
    "columns": ["ret_5m", "vwap_distance"],
    "weights": [0.5, 0.5],
    "abs": true
  }
}
```

If omitted → current behavior (unlimited signals, single-position sequencing only).

### 3.3 Execution sim (unchanged logic)

- Still **one position at a time** (`blocked_until_min` after exit)
- With only 3 **allowed** entry minutes per day, natural trade count drops to **≤ 3/day** (often 1–3 if first trade blocks until exit)

**Clarification:** 3 entries/day cap is on **entry signals selected**, not “3 overlapping positions.”

### 3.4 Audit thresholds (adjust for lower N)

| Gate | Unfiltered R1S | Top-3/day (suggested) |
|------|----------------|------------------------|
| `min_trades` | 30 / quarter | **20** / quarter (~0.3/day × 63 days) |
| `max_trades` | 100000 | 200 |
| t, CI, outlier, WR 40% | same | same |

Document any threshold change in matrix JSON.

---

## 4. Rules to backtest (wave A)

| Rule ID | Entry | Selection | Exit |
|---------|-------|-----------|------|
| `R1S_TOP3_S0_FIRST` | R1S | First 3 signals/day | E0 baseline |
| `R1S_TOP3_S1_RET5M` | R1S | Top 3 by \|ret_5m\| | E0 baseline |
| `R1S_TOP3_S3_COMPOSITE` | R1S | Top 3 composite score | E0 baseline |
| `R1S_UNLIMITED` | R1S | (control) | E0 baseline |

**Optional wave B** (only if wave A beats control on PASS count or Aug–Oct):

- Best scorer + **E3 trail** or **underlying 0.30% stop**
- Best scorer + soft `regime_vix_close < 18` as **score boost**, not hard disqualifier

---

## 5. Test matrix

**File:** `rule_matrix_r1s_top3_daily.json`

- **4 rules** × **19 windows** = **76 cells**
- Windows: same 17Q + `2024_may_jul` + `2024_aug_oct`

**VM command:**

```bash
python -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_r1s_top3_daily.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/r1s_top3_$(date +%Y%m%d)
```

---

## 6. Success criteria (go / no-go)

| Outcome | Decision |
|---------|----------|
| Top-3 scorer has **≥ 7/17** PASS quarters (beat R1S 6/17) | Strong — adopt scorer for paper |
| **May–Jul AND Aug–Oct** both PASS for one variant | Best case — candidate for paper + manual gate |
| Trade count ~40–80/q, WR **≥ 55%**, t ↑ vs unlimited | Quality thesis validated |
| Top-3 **worse** than unlimited on PASS count | Keep unlimited; top-3 hypothesis rejected |
| PASS count same but **higher** net w/o top-5 | Still valuable — more robust edge |

**Do not deploy capital** on top-3 until dual-window or 17Q improvement is clear.

---

## 7. Ops plan (2–3 days)

| Day | Task |
|-----|------|
| **D1** | Implement `trade_selection.py` + rule schema fields + unit tests (synthetic day with 10 signals → 3 kept) |
| **D1** | Wire into `run_backtest.py` after `generate_signals` |
| **D2** | Add 4 rule JSONs + matrix; run pytest; deploy to ML VM |
| **D2** | Run 76-cell sweep (~1–2 h on VM) |
| **D3** | Compare leaderboard vs `trader_wave1` / `r1s_history`; update PROJECT_STATUS |
| **D3** | If winner: doc operator sheet “max 3 fades/day”; optional paper path |

---

## 8. Manual regime (unchanged)

Top-3 does **not** replace week-level gate:

- **Week OFF:** VIX elevated, macro stress, no calm drift  
- **Week ON:** run bot/rule; engine enforces **≤ 3 entries/day** automatically  

---

## 9. Risks

| Risk | Mitigation |
|------|------------|
| Too few trades → fail `min_trades` | Lower min to 20 in matrix |
| “Best” score overfits 2020–2024 | Prefer S1/S3 over exotic; compare to S0 |
| First signal blocks day — only 1 trade | Expected sometimes; still ≤3 entries if exits fast |
| Aug–Oct still FAIL | Accept; combine with manual OFF weeks |

---

## 10. TL;DR

1. Keep **R1S entry**; add **ranked top-3-per-day** filter before simulation.  
2. Test **3 scorers + unlimited control** on **76 cells**.  
3. Win if **more PASS quarters** or **dual-window PASS** vs today’s R1S.  
4. Then paper with **manual calm-week gate + max 3/day in code**.

### Implemented (2026-05-20)

- `ml_pipeline_2/scripts/rules_pipeline/trade_selection.py`
- Rule fields: `max_trades_per_day`, `trade_score`
- Configs: `ml_pipeline_2/configs/rules/r1s_top3/*.json`
- Matrix: `rule_matrix_r1s_top3_daily.json` (76 cells, `min_trades: 20`)

### Results (`r1s_top3_20260520`) — **DONE** (76 cells, 15 PASS, 0 ERROR)

Full corpus **2020-08-03 → 2024-10-31**: 4 rules × 19 windows (17 quarterly + `2024_may_jul` + `2024_aug_oct`). Not a single full-span cell — same design as `r1s_history`.

#### PASS count (quarterly windows only)

| Rule | Quarterly PASS | Dual window (May–Jul + Aug–Oct) | Notes |
|------|------------------|----------------------------------|-------|
| **R1S_TOP3_S3_COMPOSITE** | **8 / 17** | **Both PASS** | Best variant; beats baseline 6/17 |
| R1S_TOP3_S0_FIRST | 2 / 17 | FAIL | |
| R1S_TOP3_S1_RET5M | 1 / 17 | May–Jul only | |
| R1S_UNLIMITED_CONTROL | **0 / 17** | FAIL | High t in places but fails WR / net w/o top-5 gates |

**S3 PASS quarters:** 2021_q1, 2021_q2, 2021_q4, 2022_q1, 2022_q4, 2024_q1, 2024_q2, 2024_q3.

**S3 FAIL quarters:** 2020_aug_dec, 2021_q3, 2022_q2, 2022_q3, 2023_q1–q4, 2024_oct.

#### Go / no-go (final)

| Criterion | Result |
|-----------|--------|
| ≥ 7/17 PASS | **YES** (8/17) |
| Dual-window PASS | **YES** |
| Beats unlimited control | **YES** (0/17 unlimited) |
| **Decision** | **GO for paper** with `R1S_TOP3_S3_COMPOSITE` + manual week gate |

Paper wiring: profile `r1s_top3_paper_v1` — see `docs/R1S_TOP3_OPERATOR.md`.

### Monthly sweep (2020-08 → 2024-10)

**Matrix:** `rule_matrix_r1s_top3_monthly.json` — **51 calendar months**, 2 rules (S3 + unlimited control) = **102 cells**.

`min_trades` lowered to **10** per month (~20 sessions × ≤3 entries).

Generate / rebuild windows:

```bash
python ml_pipeline_2/scripts/rules_pipeline/generate_monthly_windows.py \
  -o ml_pipeline_2/scripts/rules_pipeline/_monthly_windows_2020_2024.json
python ml_pipeline_2/scripts/rules_pipeline/build_r1s_top3_monthly_matrix.py
```

**VM run:**

```bash
python -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_r1s_top3_monthly.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/r1s_top3_monthly_YYYYMMDD
```

Summarize:

```bash
python ml_pipeline_2/scripts/rules_pipeline/summarize_monthly_leaderboard.py \
  ml_pipeline_2/artifacts/rules_runs/r1s_top3_monthly_YYYYMMDD/leaderboard.md
```

### Results (`r1s_top3_monthly_20260520`) — **102 cells, 7 PASS, 0 ERROR**

All **7 PASS** are **S3 only** (unlimited **0/51** months). Monthly audit is stricter (`min_trades: 10` + short window).

| Month | n | t | WR |
|-------|---:|---:|---:|
| 2020_08 | 11 | +3.77 | 91% |
| 2021_02 | 13 | +2.14 | 92% |
| 2021_08 | 12 | +2.36 | 75% |
| 2022_12 | 17 | +2.68 | 76% |
| 2024_01 | 16 | +2.84 | 81% |
| 2024_05 | 20 | +2.88 | 80% |
| 2024_07 | 15 | +3.07 | 87% |

Interpretation: edge clusters in **calm months** (matches quarterly PASS map); most months FAIL on small-n gates or outlier survival, not necessarily negative mean.

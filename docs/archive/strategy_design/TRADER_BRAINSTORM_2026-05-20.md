# Trader desk brainstorm — BankNifty intraday options

**Date:** 2026-05-20  
**Purpose:** Fresh multi-trader thinking on *how* to trade and *how* to exit — refine ideas from data evidence, not from ML hope.  
**Next step:** Pick 3–5 specs → rule JSON + exit variants → 17Q audit (same gates as R1S).

**Evidence we do not ignore:**
- Long ATM @ 1m bleeds theta (rules v1/v2: long and fade both lost).
- **R1S** (sell CE on ORB-down) is the only audit PASS candidate — **6/17 calm quarters**.
- Automated daily/VIX gates failed (v1/v2/v3). **Participation** may stay human.
- Velocity fires on ~0.3% of rows — use as **alert**, not scan-every-minute.
- `vwap_distance` is **fractional**, not points.

---

## 0. Desk agreement (10 minutes)

| # | Statement | Dissent |
|---|-----------|---------|
| 1 | We are **premium sellers** on calm days, **premium buyers** only on rare aligned momentum | Momentum trader wants more long book |
| 2 | **One trade quality > ten marginal fires** | Quant wants sample size |
| 3 | **Exit engineering** is as important as entry (R1S fails Aug–Oct on stops, not only entry) | — |
| 4 | **Week-level gate** stays operator/manual until something beats 6/17 PASS | — |
| 5 | Every idea must be writable as JSON rule + mechanical exit for audit | Discretionary purist |

---

## 1. Four trader voices (structured debate)

### Voice A — “Theta farmer” (short premium)

**Thesis:** BNF intraday mean-reverts in grind-up weeks. Edge = sell overpriced ATM options on **failed** directional moves.

**Best setups:**
- ORB-down but spot still “wrong side” of VWAP only temporarily → **sell CE** (R1S).
- Midday stretch above VWAP + RSI hot → **sell CE** (not buy PE).
- PCR stable, VIX &lt; 18, no acceleration in `vel_price_delta_30m`.

**Exit philosophy:**
- Take 40–50% of credit fast; runner only with **spot reclaim VWAP** trail.
- Wide premium stop (80–100%) *or* underlying 0.30% — macro kills tight premium stops.

**Challenge from B:** “You die in 2022 when break is real.”  
**Reply:** That’s week gate, not tighter ORB. Don’t sell CE when VIX &gt; 18 and 60d return negative.

---

### Voice B — “Flow momentum” (directional long options)

**Thesis:** When futures OI + volume + price align, buy ATM option for 5–15m burst.

**Best setups:**
- ORB **with** `vel_price_acceleration`, PCR delta in direction, `fut_flow_oi_change_5m` extreme.
- **Max 2 trades/day**, 9:30–11:00 only.

**Exit philosophy:**
- Tight 25% premium stop, 35% target, 12-bar time stop.

**Challenge from A:** “Backtest said longs bleed at 1m.”  
**Reply:** Frequency was too high; **conditional** momentum only when velocity + ORB agree — not tested cleanly yet (Play B′).

---

### Voice C — “VWAP + structure” (midday mean reversion)

**Thesis:** After 10:30, trade **distance from VWAP** in non-trending ATR (`osc_atr_percentile` mid band).

**Best setups:**
- `|vwap_distance| > X` (fractional: e.g. 0.003 = 0.3%) + RSI extreme → sell rich wing (CE if above VWAP).
- Confirm `oi_atm_pe_ce_ratio` not screaming trend.

**Exit:** touch VWAP or 50% credit on short; 18m time.

**Challenge from D:** “Chop gives 50% WR, no edge.”  
**Reply:** Disqualify when `osc_atr_percentile > 0.75` or opening drive day.

---

### Voice D — “Velocity sniper” (sparse high conviction)

**Thesis:** Your edge lives in **session deltas** at anchor times — treat like news candle.

**Best setups:**
- On minutes where `vel_pcr_delta_30m` or `vel_oi_ratio_delta_30m` is non-NaN: if PCR rip + price below VWAP → sell CE once per anchor window.
- **Cap 1 signal per 30m anchor**, not every row.

**Exit:** 35% credit target, 10m time, no runner.

**Challenge from desk:** “0.3% coverage — audit needs 30 trades/quarter.”  
**Reply:** Combine with A; velocity is **filter**, not standalone book.

---

## 2. Synthesis — three “books” not twelve rules

Instead of 20 weak rules, run **three playbooks** with shared risk:

```
┌─────────────────────────────────────────────────────────┐
│  WEEK GATE (manual): calm drift / elevated vol / flat   │
└──────────────────────────┬──────────────────────────────┘
                           │
     ┌─────────────────────┼─────────────────────┐
     ▼                     ▼                     ▼
 BOOK 1 FADE          BOOK 2 DRIVE         BOOK 3 VWAP
 (sell premium)       (long option)        (sell premium)
 calm only            rare, AM only        midday calm
 R1S family           ORB+velocity         stretch fade
```

**Mutual exclusion:** If BOOK 1 fired today, BOOK 2 off (same day conflict).

---

## 3. Refined ideas — top 5 for backtest

Priority order = expected ROI of test given evidence.

### IDEA 1 — R1S-Alpha (fade core + better exit) ★★★★★

**Hypothesis:** R1S entry is sound; **Aug–Oct failure** is exit/stop in vol expansion, not entry.

| Field | Spec |
|-------|------|
| Entry | Same as R1S: OR ready, break down, `ret_5m<0`, `vwap_distance<0` |
| Direction | SELL_ATM_CE |
| Disqualifiers | 9:30–14:30, expiry; optional `regime_vix_close > 18` as **soft** (test on/off) |
| Stop | Premium +100% **OR** underlying +0.30% against (whichever first) |
| Target | 50% credit on 60% size |
| **New exit** | **VWAP reclaim:** if `vwap_distance >= 0` for 3 consecutive bars → close all |
| **Trail** | After 30% credit captured, trail remaining by 25% premium giveback from best |
| Time | 25m; EOD 15:20 |

**Variants to matrix:** exit = mechanical only | +VWAP reclaim | +trail.

---

### IDEA 2 — ORB-down + flow “real break” filter ★★★★

**Hypothesis:** Lose in stress quarters because break is **real** — filter when flow confirms continuation.

| Field | Spec |
|-------|------|
| Entry | ORB-down + below VWAP + `ret_5m<0` |
| **Skip fade when** | `vel_price_delta_30m > 0` AND `pcr_change_5m > 0` (or `oi_atm_pe_minus_ce_5m` rising) |
| Direction | SELL_CE when NOT skip; else no trade |
| Exit | Same as IDEA 1 |

**Trader logic:** “I don’t sell CE into accelerating down + puts building.”

---

### IDEA 3 — Opening drive long (sparse momentum) ★★★

**Hypothesis:** Long options work **only** when ORB + velocity + OI align — 30–80 trades/quarter max.

| Field | Spec |
|-------|------|
| Entry | OR ready, break **up**, `ret_5m>0`, above VWAP, `vel_price_acceleration>0`, `fut_flow_rel_volume_20 > 1.2` |
| Direction | BUY_ATM_CE (mirror PE for break down if tested) |
| Disqualifiers | after 11:00, expiry, `osc_atr_percentile > 0.8` |
| Stop | 25% premium |
| Target | 40% premium |
| Time | 12 bars |

**Expectation:** May fail audit — but tests “trader long” honestly.

---

### IDEA 4 — VWAP stretch fade (midday seller) ★★★

| Field | Spec |
|-------|------|
| Entry | `time_minute_of_day >= 630` (10:30), `vwap_distance > 0.004`, `osc_rsi_14 > 70`, `osc_atr_percentile < 0.7` |
| Direction | SELL_ATM_CE |
| Disqualifiers | expiry, ORB break down same day (conflict with IDEA 1) |
| Stop | +70% premium |
| Target | 45% credit |
| Exit signal | `vwap_distance < 0.001` |
| Time | 18m |

**Fix from v1:** never use `vwap_distance > 100` (points bug).

---

### IDEA 5 — Velocity anchor fade ★★

| Field | Spec |
|-------|------|
| Entry | `vel_pcr_delta_30m` not NaN AND `vel_pcr_delta_30m > T` AND price below VWAP AND `vel_pcr_trend_direction` bearish |
| Direction | SELL_CE |
| Cooldown | 30m after trade |
| Exit | 40% credit, 10m |

**Risk:** low N per quarter — use as **modifier** on IDEA 1, not solo.

---

## 4. Exit laboratory (desk consensus)

Test these **exit profiles** across IDEA 1–2:

| Profile | Stop | Target | Time | Special |
|---------|------|--------|------|---------|
| E0 (R1S baseline) | 100% prem | 50% | 20m | — |
| E1 | 100% prem / 0.30% und | 50% | 25m | — |
| E2 | E1 + VWAP reclaim 3bar | 50% | 25m | spot thesis exit |
| E3 | E1 + trail 25% giveback after 30% gain | 50% partial | 25m | runner management |
| E4 | 70% prem stop | 40% | 15m | tighter farmer |

**Success metric:** Aug–Oct 2024 cell + net w/o top5 ≥ 0, not only May–Jul.

---

## 5. Participation checklist (human, week-level)

Until automated gate beats 6/17 PASS:

| Trade BOOK 1/4 (sell fade) | Trade BOOK 2 (long drive) | Flat |
|----------------------------|---------------------------|------|
| VIX &lt; 17–18 | VIX any but structure clear | VIX &gt; 20 |
| Spot ≥ 20d MA or flat | Strong ORB + volume | Banking/macro week |
| No gap &gt; 0.8% against fade | First 90m only | Expiry chaos optional flat |

---

## 6. What we kill (save time)

| Idea | Reason |
|------|--------|
| Symmetric long ORB (R1/R2 v1) | Theta + audit failed |
| `ctx_is_high_vix_day` alone as gate | Killed 2021 Q1; wrong fire rate |
| `regime_rv20` disqualifier | PASS quarters higher rv20 |
| Per-minute velocity in AND chain | 99.7% NaN |
| ML entry at 1m without exit redesign | Audit failed |
| 0DTE iron condor etc. | Different product; not in data story |

---

## 7. Backtest wave 1 (recommended)

**6 cells** (manageable):

| Rule ID | Entry family | Exit profiles |
|---------|--------------|---------------|
| T1 | IDEA 1 R1S-Alpha | E0, E2, E3 |
| T2 | IDEA 2 flow-filter fade | E0, E2 |

Windows: 17Q + `2024_may_jul` + `2024_aug_oct`.

**Pass bar:** ≥ baseline R1S (6 PASS) **or** dual-window both PASS with net w/o top5 ≥ 0.

### Wave 1 implementation (2026-05-20)

- Configs: `ml_pipeline_2/configs/rules/trader_wave1/*.json`
- Matrix: `rule_matrix_trader_wave1.json` → **114 cells** on ML VM
- Log: `/tmp/trader_wave1.log` → `artifacts/rules_runs/trader_wave1_20260520/leaderboard.md`
- New sim features: `disqualifier_all_of`, `signal_exits` (VWAP), `trail_*`, `underlying_stop_pct`

### Wave 1 results (complete — 114 cells, 28 PASS rows)

| Rule | ~PASS quarters (of 19) | May–Jul 2024 | Aug–Oct 2024 |
|------|------------------------|--------------|--------------|
| T1/T2 **E0** (R1S baseline) | 6 (same as R1S) | **PASS** | **FAIL** |
| T1/T2 **E3** (trail) | 6 | **PASS** | FAIL |
| T1/T2 **E2** (VWAP reclaim) | 4 | FAIL (outlier) | FAIL |

**T1 vs T2:** identical stats on every window — flow filter (`vel_price_delta_30m` + `pcr_change_5m`) did not bite at entry minutes (sparse NaN).

**Verdict:** Exit tweaks help some quarters (E3 ≈ E0); **E2 VWAP exit hurts outlier survival**. **None** fix Aug–Oct 2024. Best deploy candidate remains **T1_E0 / R1S** with **manual week gate**, not E2/E3 automation.

---

## 8. Open questions for real traders in the room

1. VIX gate: **17 or 18 or 20** for “no sell CE”?
2. Max trades per day: **1, 2, or 3** for fade book?
3. On ORB-down fade: exit on **VWAP reclaim** vs **fixed 50% credit** — which matches your live tape?
4. Do you ever **buy PE** on ORB-down (real break) — should IDEA 3 include PE leg?
5. Expiry day: flat entirely or half size?

---

## 9. One-page “best idea” if desk votes now

**Trade:** Calm-week **sell ATM CE** on opening-range break **down** when price still below VWAP and 5m return negative, **unless** 30m price velocity and PCR show real continuation.

**Manage:** Half at 50% credit; rest exit on **VWAP reclaim** or 25% premium trail; hard abort if underlying +0.30% against; **no** new entries after 14:30.

**Do not trade:** VIX elevated weeks, macro stress (manual), expiry if uncomfortable.

This is **IDEA 1 + IDEA 2 + exit E2/E3** — evolution of R1S, not a new religion.

---

## 10. Links

- [TRADER_HANDOVER.md](TRADER_HANDOVER.md) — feature catalog  
- [R1S_REGIME_EXPERIMENT.md](R1S_REGIME_EXPERIMENT.md) — regime gate results  
- [PROJECT_STATUS_2026-05-20.md](PROJECT_STATUS_2026-05-20.md) — full project verdict  
- [RULES_V1_PROPOSED.md](RULES_V1_PROPOSED.md) — what failed and why  

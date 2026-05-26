# R1S Sell-Side Hypothesis — Pre-registered spec

**Status:** Pre-registered — do NOT change pass gates after first replay result  
**Date:** 2026-05-26  
**Author:** Claude Code (session 2026-05-26)  
**Related:** `memory/project_r1s_regime_finding.md` · `docs/HUMAN_STYLE_STRATEGY_SPEC_2026-05.md`

---

## Why this doc exists

The E1–E8 arc failed OOS on every long-ATM config. Root cause: theta drag is structural.
The only PASS-class edge found in the arc is **R1S** (short ATM CE, sell-side premium capture
on bear/chop days in calm-vol regimes). That finding came from a 17-quarter rule sweep and
showed regime-conditional profit (6/17 quarters PASS, all in calm VIX periods).

**This document pre-registers the exact falsifiable test before any code is written.**
Pass gates, filter formula, and OOS windows are fixed here. They cannot be adjusted after
seeing the first replay result.

---

## The edge finding (discovery, 2026-05-20)

Rule: short ATM CE on sessions with (ORB-down) + (bearish momentum) + (below VWAP).

| Quarter group | n quarters | Finding |
|--------------|-----------|---------|
| Calm-vol PASS | 6 | PF 1.4–2.2, top-5-day share 55–69% — distributed edge |
| High-vol FAIL | 11 | Net P&L destroyed by stop-loss blowouts; top-5 share > 100% (outside top 5, net negative) |

All FAIL quarters map to identifiable macro events: Ukraine (2022), SVB/Credit Suisse
(2023-Q1/Q2), Aug carry-unwind + Iran escalation (2024-Q3/Oct).

**The mechanism is clear:** short premium captures theta in drift/chop; fails
catastrophically when IV spikes during macro shocks (stops hit at avg −24%).

---

## Hypothesis

> **H1:** R1S with a `daily_vix < 16.0` filter applied has a profitable and statistically
> meaningful edge in calm-regime quarters. Specifically: the filter correctly labels the
> 2024-Q3 and 2024-Oct quarters as high-vol (trading frequency drops ≥ 60% vs unfiltered),
> preventing the catastrophic drawdown observed in the unfiltered sweep.

> **H0 (null, falsifies H1):** The VIX filter either (a) does not reduce trading on the FAIL
> quarters, or (b) reduces trading on PASS quarters equally, eliminating the edge.

---

## Exact rule definition

### Entry conditions (ALL must be true at bar evaluation)

| Field | Condition | Source |
|-------|-----------|--------|
| ORB direction | ORB broken DOWN (`orb_low` crossed) | `snapshot.orb_low_broken` |
| Spot vs VWAP | Spot < VWAP | `snapshot.vwap_futures` |
| Session momentum | BankNifty futures change < −0.25% from open | `snapshot.fut_open` vs `snapshot.fut_close` |
| Time window | 09:25–10:15 IST only | `snapshot.timestamp` |
| Daily VIX filter | `daily_india_vix < 16.0` | `snapshot.vix` |
| Direction | CE short only (selling) | hardcoded |
| Strike | ATM CE at session open | `snapshot.atm_ce_strike` |

### Exit conditions (first to trigger)

| Exit | Condition |
|------|-----------|
| Target | Premium falls ≥ 25% from entry (theta capture) |
| Stop | Premium rises ≥ 15% from entry (adverse IV move) |
| Time stop | EOD — 15:15 IST (do NOT hold overnight) |

### Risk sizing

- 1 lot per trade (do not scale until OOS verified)
- Maximum 2 trades per session
- No re-entry after stop-loss in same session

---

## In-sample window (IS)

**Quarters used for filter calibration:** 2020-Q3 through 2023-Q4 (14 quarters)

From the discovery sweep, these quarters contain:
- PASS: 2020-Q3/Q4, 2021-Q1, 2021-Q4, 2023-Q3
- FAIL: all 2022, 2021-Q2/Q3, 2023-Q1/Q2/Q4

The VIX threshold of 16.0 was calibrated to match the known FAIL/PASS labels in this window.
If replaying IS: the filter must produce n ≥ 30 trades on PASS quarters before OOS proceeds.

---

## Pre-registered OOS window

**OOS quarters (SEALED — do not adjust gates after seeing results):**

| Quarter | Expected regime | Basis |
|---------|----------------|-------|
| 2024-Q1 (Jan–Mar) | CALM — should trade profitably | Strong market, low VIX |
| 2024-Q2 (Apr–Jun) | CALM — should trade profitably | Drift up, elections |
| 2024-Q3 (Jul–Sep) | HIGH-VOL — filter should suppress trading | Aug carry-unwind event |
| 2024-Oct | HIGH-VOL — filter should suppress trading | Iran-Israel escalation |

The OOS test has two sub-tests:

**OOS-A (calm quarters: 2024-Q1 + Q2):**
The filter must allow trading and the rule must be profitable.

**OOS-B (high-vol quarters: 2024-Q3 + Oct):**
The filter must suppress ≥ 60% of trades that would have fired unfiltered.
Remaining trades must not produce net drawdown > −5% cap PnL.

---

## Pass gates (pre-registered, immutable)

### Gate 1 — IS filter validation (must pass before OOS)

| Metric | Gate |
|--------|------|
| IS calm-quarter PF | ≥ 1.30 |
| IS calm-quarter n | ≥ 30 trades |
| IS calm-quarter bootstrap CI lower bound | ≥ 1.00 |
| IS high-vol trade reduction | ≥ 60% fewer trades vs unfiltered |
| IS top-5-day share (calm quarters) | ≤ 80% (edge must be distributed) |

### Gate 2 — OOS-A (calm quarters: 2024-Q1 + Q2 combined)

| Metric | Gate |
|--------|------|
| OOS-A PF | ≥ 1.20 (relaxed slightly — holdout penalty) |
| OOS-A n | ≥ 20 trades |
| OOS-A cap PnL | ≥ 0% |

### Gate 3 — OOS-B (high-vol quarters: 2024-Q3 + Oct)

| Metric | Gate |
|--------|------|
| High-vol trade reduction | ≥ 60% vs unfiltered |
| High-vol cap PnL | ≥ −5% (filter prevents catastrophe) |

**Verdict rule:** PASS requires Gate 1 AND Gate 2 AND Gate 3.
FAIL on any single gate → hypothesis falsified. Do not adjust thresholds.

---

## What falsification looks like

| Outcome | Interpretation |
|---------|---------------|
| VIX never exceeds 16 on "FAIL" quarters | Threshold too low — R1S has no implementable filter |
| Filter reduces IS calm-quarter n below 30 | Over-filtering — edge disappears |
| OOS-A PF < 1.0 | Edge was IS noise, no OOS carry-through |
| OOS-B trade reduction < 60% | VIX-based filter doesn't capture the regime signal |
| OOS-B drawdown > −5% | Filter insufficient — macro risk persists |

If falsified: **pivot to coarser horizon (5-min entry) or NIFTY instrument** before any further
sell-side experiments. Do not tune the filter threshold to force a PASS.

---

## Implementation order (gated — do not skip)

1. **Data audit** — verify `snapshot.vix` field is populated for all IS quarters in parquet
2. **IS replay** — run R1S rule with VIX filter on 2020-Q3 to 2023-Q4; check Gate 1
3. **If Gate 1 passes** → run OOS-A (2024-Q1 + Q2); check Gate 2
4. **If Gate 2 passes** → run OOS-B (2024-Q3 + Oct); check Gate 3
5. **If all 3 gates pass** → write R1S engine story, design implementation, re-register live pass gate

No code is written until Gate 1 passes. No OOS run until IS confirmed.

---

## VIX field verification (pre-implementation check)

Before running any replay, confirm `snapshot.vix` is available:

```python
# In snapshot_accessor.py — check field exists and is populated
snap.vix  # should return float or None; None means field absent in parquet
```

If `vix` is None across most bars, use `osc_atr_daily_percentile < 0.75` as fallback filter.
The fallback threshold must be chosen on IS data before OOS is touched.

---

## Changelog

| Date | Change |
|------|--------|
| 2026-05-26 | Initial pre-registration. Gates, windows, and filter formula fixed. |

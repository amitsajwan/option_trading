# Best-Setup Plan — after the 2026-06-11 triangulation

> Status: **no profitable directional-buy config exists on current data** (proven from 7 angles). Real money stays **OFF**. This is the plan to find a setup that survives *measured* cost, plus the overnight work in flight.

## 1. Verified walls (don't re-test these — they're settled)
- **5-min direction ≈ 50% coin-flip** — holds even when 91% trend-aligned, in TREND regime, at 5/10/30-min horizons, and for chase-vs-pullback entry timing. (June 1, −652pt day.)
- **Entry model is regime-dependent / false in low-vol** — only 17% of ≥0.85 bars saw ≥100pt in 5 min; avg 5-min range 67pt vs the ~110pt it claims.
- **Hold-the-move refuted** — win% collapses 57%→26% as hold 3→13min; no fat tail (best win +5.9%). Scalper 3-min is least-bad.
- **"Huge" days are gross illusions** — June 1 logged +5.35% gross but −43%/−67% net (24 trades × cost). Dashboard P&L ≠ net.

## 2. The ONE real edge we observed
The **daily trend drift** (June 1 = −652pt) is real, and our trend *call* was right (91% PE). We destroy it by **scalping it into 3-min slices and paying cost on each**. The edge lives at the **daily** scale; we shred it at the **3-min** scale.

## 3. THE load-bearing unknown — measure before deciding anything
`COST=3%` was an **assumption**, not data. Components: brokerage+statutory ~0.3% (fixed) + **bid-ask slippage ~1–2.5% (variable, dominant)**. One real live fill ≈ **~1%**. 
**TODO #1 (highest priority):** measure true round-trip cost from the snapshot ATM **bid/ask** at trade times + the exact Dhan charge formula; re-score every result at that number. At ~1% the whole picture may shift from "dead" to "viable if few trades."

## 4. Two candidate strategies (both sidestep 5-min direction)
### A. Trend-Hold (natural fit for what we saw)
- **Once-a-day** call: "is today a trend day, and which way?" (a daily decision, not a 5-min one) → take **one** trend-aligned position, **hold to EOD**.
- **First test:** June 1 single PE held to EOD vs the 24 scalps — does it capture the 652pt drift net of theta+cost?
- If yes → build a trend-day detector (open-range break, VWAP slope, regime=TREND persistence) + 1-position/day engine.

### B. Sell Premium (gets paid for the 83% non-moves)
- The entry model being "false" (predicting moves that don't come) **is the seller's edge**. Theta works *for* us.
- **Defined-risk credit spread** fits ₹109k margin (naked/futures need ₹1.5L+).
- **First test:** on the same days, sell the opposite-side credit spread on a 55% lean; measure net incl. real spread-fill cost + the tail days.

## 5. Overnight (in flight on `option-trading-ml-01`)
- Retraining **E6-successor 5-min entry models @ 0.10 / 0.20 / 0.30** (`min_pct` .001/.002/.003), `fo_comprehensive` features, isotonic-calibrated, full ship-gates. Orchestrator: `ops/gcp/run_entry_fullfeature_retrain_vm.sh` (tmux `entry_fullfeature`). Bundles → `ml_pipeline_2/artifacts/entry_only/published_comprehensive/`, reports `*_report.json`.
- **Check next session:** `bash ops/gcp/run_entry_fullfeature_retrain_vm.sh status`; pick any bundle with `ship_gates_all_pass=true` that beats `020pct` on **separation + drop-outlier + entries/day**.
- **Temper expectations:** entry is the *magnitude* leg; direction(50%)+cost are the bigger walls. A better entry model **refines**, it does not by itself make the system profitable. **Then stop the ML VM** (`gcloud compute instances stop option-trading-ml-01`).

## 6. Decision gate (the rule)
A setup graduates to **paper** only if it shows **positive net after *measured* cost + drop-outlier** in sim. It graduates to **1-lot real** only after positive paper over multiple days. **Real money stays OFF** until then. Capital (the extra ₹60k) is added only to *scale a proven edge*, never to rescue an unproven one.

## 7. Next-session order of operations
1. Measure real cost (TODO #1) — re-score everything.
2. Run the **Trend-Hold June-1 test** (A) — the most promising, cheapest test.
3. Evaluate overnight entry models (ship-gates).
4. If A works → build trend-day detector. If not → stand up the **Sell-Premium** test (B).

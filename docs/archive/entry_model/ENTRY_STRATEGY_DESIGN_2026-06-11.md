# Entry & Strategy Design — the rigorous pass (2026-06-11)

Same bench we used for exits (trader + quant + scientist), now on **entry + when-to-trade + stops**. Everything here is forced to obey what we *proved* tonight.

## 0. The reframe that changes everything
We kept trying to win by being **right about direction**. We proved that's ~50% (coin-flip) at our horizon — unwinnable. **So stop pricing the edge as direction accuracy. Price it as ASYMMETRY (reward-to-risk from *location*).**

The math that killed us (buying): `W≈L≈1.4%, C≈3% → E<0` even at perfect direction.
The same math, **flipped by location + trend**:
```
E = p·W − (1−p)·L − C
Trend-Hold:  W = drift capture (5–10%),  L = tight invalidation (1–2%),  trades/day = 1
  even at p=0.45:  0.45·8 − 0.55·1.5 − 1.0 = 3.6 − 0.83 − 1.0 = +1.8% / trade-day
```
**The edge is not "I know the direction." It's "I risk 1 to make 8, and I only need to be right 4 times in 10."** A tight **level-based stop** + a **trend-sized target** + **few trades** is the whole game. This is exactly what the exit brainstorm wanted but scalping couldn't deliver.

## 1. The three questions (entry decision framework)
**(a) WHEN to TAKE a trade — is today even tradeable?**
- Trend-day confirmers (need **≥3** to agree): opening-range break (price clears the first 30–45 min hi/lo), **VWAP slope** persistently one way, **EMA stack** aligned (9>21>50 or inverse), **trend-strength** (ADX-like / consecutive higher-highs or lower-lows), regime=TREND persisting ≥N bars, supportive **OI shift / max-pain drift**.
- Range/sell-day confirmers: price oscillating around VWAP, flat EMA stack, price pinned near an **OI wall / max-pain**, low realized vol.

**(b) WHEN to AVOID — sit on hands.**
- Inside the opening range (undefined). MID/CHOP with no level. Price **extended** far from any level (chasing). Lunch lull (≈12:00–13:30 IST). Last 30 min for *new* trend entries. Expiry-day gamma/theta cliff. Conflicting confirmers. A big **opposing OI wall** right ahead of the target.

**(c) WHEN to ENTER — the trigger (this is where we were wrong).**
- **Never enter on the momentum candle** (that's the chase → local exhaustion → reversal, proven 49%).
- **Enter on the FIRST PULLBACK to a level** in the trend direction: pullback to **VWAP**, the **broken ORB level** (retest), an **EMA**, prev-day level, or round number — with a **rejection/hold** confirmation (the level holds, momentum doesn't make a new extreme against us).
- **Use a LIMIT order at the level** → also cuts slippage cost (we cross less spread).

## 2. Candidate strategies (each fully specified → SIM-ready)

### S1 — Trend-Day Rider  (capture the daily drift; the headline bet)
- **Thesis:** the one real edge — daily trend drift; asymmetry from a level stop.
- **Take when:** ≥3 trend confirmers (§1a). **Avoid:** §1b.
- **Enter:** first pullback to VWAP/ORB-retest/EMA in trend dir, on a hold, **limit at the level**. 1 (max 2) entries/day.
- **Direction:** the **day trend** (not the 5-min model).
- **Stop (invalidation):** structural — just beyond the level / swing pivot (~20–35pt underlying). Hard floor as % backup.
- **Exit:** **trend-trail** behind structure (higher-lows for CE / lower-highs for PE) **OR** exit on **trend-break** (VWAP reclaim against us, EMA cross, regime→CHOP). Optional partial at +2R, runner to EOD. Time-stop EOD.
- **Falsify:** if level-stopped trades lose more than the trail-winners make over a month → asymmetry isn't real.

### S2 — Pullback-to-Level (more trades, tighter)
- **Thesis:** the 30-min pullback hint (60%) + limit-at-level slippage savings; trade the trend in 2–3 controlled clips instead of one hold.
- **Take/Avoid:** trend day, price AT a level (not extended).
- **Enter:** limit at VWAP/ORB/wall in trend dir; confirmation = low-volume pullback + level hold.
- **Stop:** tight, just beyond level (~15–25pt). **Exit:** target = next level / +2R, trail; short time-stop. Budget 2–3/day.
- **Falsify:** if pullback entries ≯ chase entries net (we showed only 51% @5min — needs the level filter to lift it).

### S3 — Premium Seller (get paid for the 83% non-moves)
- **Thesis:** entry model "false" = no move = **seller's income**; theta tailwind; defined risk.
- **Take when:** range/MID day, low realized vol, price near wall/max-pain. **Avoid:** trend days, events, expiry gamma.
- **Enter:** **credit spread** (defined risk, fits ₹109k) on the side away from the wall / opposite the weak lean.
- **Stop:** spread value 2× credit, or underlying breaks short strike/wall. **Exit:** 50% max profit, or theta target, or time-stop. Budget 1–2/day.
- **Falsify:** the tail days (the 17% that DO move) erase the collected premium.

### S4 — Regime-Switched Book (the complete system)
- **TREND day → S1/S2 (buy direction). RANGE day → S3 (sell premium).** A regime classifier flips the active engine. S1 and S3 are natural complements (opposite regimes), so the book is rarely idle and never fighting the regime.

## 3. Exit design — the part you flagged (stop / hard-stop / conditional / trail)
The exit brainstorm's verdict (scalper least-bad, hold refuted) was for **direction-buying random bars**. For **level-based trend entries the exit logic changes**, and *this* is where stops earn their keep:
- **Primary stop = INVALIDATION, not a %.** The level defines "I'm wrong" → tiny, structural risk (the asymmetry source). The % hard-stop is only a backstop.
- **Conditional exits (new):** exit on **regime flip** (TREND→CHOP), **VWAP reclaim** against position, **EMA cross**, **momentum divergence**, or **opposing-wall hit**. These are *thesis-invalidation* exits, not P&L exits.
- **Trailing:** structure-trail (behind swing pivots), **not** a fixed 0.5% — let the trend run, exit when the trend (not the price) breaks.
- **Time:** EOD hard close (no overnight at 1 lot).

## 4. SIM verification plan (real engine, after we finalize)
For each finalized strategy, on the real replay engine (stack ON, fresh run-dir/day, **measured** cost):
- Metrics: trades/day, win%, **avg R (not %)**, gross, **net at measured cost**, drop-outlier robustness, max DD, per-day net.
- Scenario split: **trend days vs range days** separately (S1/S2 must win on trend days; S3 on range days).
- Gate: graduate to paper only if **net>0 after measured cost + drop-outlier**.

## 5. Recommendation
Finalize **S1 (Trend-Day Rider)** first — it's the direct expression of the only real edge and the asymmetry math. Build the **trend-day classifier** (§1a) + **pullback-to-level trigger** (§1c) + **structural/conditional exits** (§3), then SIM on June 1–3 + the 9 days, split trend-vs-range. S3 (Premium Seller) is the parallel track for range days. S2 is a variant of S1 to A/B once S1 is wired.

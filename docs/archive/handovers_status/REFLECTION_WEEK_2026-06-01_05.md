# Reflection Journal — Week 2026-06-01 → 06-05 (ops-sim on real mongo data)

**Source:** ops-sim replay (paper book) of `phase1_market_snapshots`, run on VM `option-trading-runtime-01`, code `4857805`. P&L = sum of per-trade premium-% (the sim's `session_pnl`). Loss tags = deterministic post-trade autopsy (`strategy_app/brain/reflection.py`).

## Day-wise P&L

| Date | Trades | Wins | Win-rate | P&L % | direction_miss | cost_miss | exit_miss | entry_miss | noise |
|---|---|---|---|---|---|---|---|---|---|
| 2026-06-01 | 4 | 2 | 50% | **+6.29** | 2 | 0 | 0 | 0 | 0 |
| 2026-06-02 | 9 | 4 | 44% | **+5.31** | 4 | 1 | 0 | 0 | 0 |
| 2026-06-03 | 14 | 8 | 57% | **+7.22** | 5 | 1 | 0 | 0 | 0 |
| 2026-06-04 | 7 | 2 | 29% | **−3.97** | 5 | 0 | 0 | 0 | 0 |
| 2026-06-05 | 3 | 1 | 33% | **−4.72** | 2 | 0 | 0 | 0 | 0 |
| **Total** | **37** | **17 (46%)** | — | **+10.13** | **18** | **2** | **0** | **0** | **0** |

## Loss-cause distribution (20 losses)

- **direction_miss: 18 / 20 = 90%** — wrong side from the start (MFE ≈ 0; price never moved our way).
- **cost_miss: 2 / 20 = 10%** — flat scalps where slippage+charges flipped a ~0 gross to a small loss.
- **exit_miss: 0**, **entry_miss: 0**, **noise: 0**.

## What's wrong (analysis)

1. **Direction is the entire problem.** 90% of losses are `direction_miss` — the option was on the wrong side and the underlying moved against it immediately (MFE ~0). The two **red days (06-04, 06-05) are 100% direction misses**.
2. **Exits are NOT the problem.** `exit_miss = 0` across 37 trades — we are *not* giving back winners. This refutes the long-standing "MFE-giveback" worry for this week; the exit stack (target/trail/time-stop) is behaving. Losses exit via `TIME_STOP` at small, controlled sizes (−2% to −4%, worst single −6.5%) — risk control is working.
3. **Green vs red is decided by direction hit-rate, not entry/exit quality.** Win days run ~50–57% direction-right and the asymmetric payoff (wins +4–5%, losses −2–4%) compounds green; red days fall to ~30% right and bleed. Same engine, same exits — only the side is wrong.
4. **Likely CE bias caught on down moves.** On 06-04/06-05 the book is almost all CE and every CE loses as direction_miss — i.e. the strategy leaned long calls into a falling/choppy tape.
5. **Honest caveat — `entry_miss = 0` is partly a data gap.** Entry sense-verdicts aren't persisted on the position yet, so the autopsy can't see a "marginal entry." So 0 entry_miss does **not** prove entries were well-selected — it means we lack the evidence to tag them. (Fix: persist entry verdicts into `decision_metrics`, then re-run.)

## Conclusion

The journal gives a clean, mechanical verdict consistent with the whole project history: **direction is THE bottleneck.** Not exits, not cost. The lever is a **direction filter / abstain-when-unclear** (trade fewer, only when the side is confident) and/or removing the structural CE bias — **not** exit tuning. The week is net +10% *despite* 90%-direction-miss losses purely because the 3 green days had enough direction-right trades; lifting direction accuracy on the 2 red days is where the P&L is.

## Location retro-analysis (the "where is price?" test)

`session_levels` (PDH=`prev_day_high`, PDL=`prev_day_low`, `week_high/low`, gap, max_pain) is **already computed in every snapshot** — but the strategy doesn't use it for direction. Joined each entry's `futures_bar.fut_close` to the levels at its entry bar. Zone = price vs PDH/PDL (band 0.15%).

**Entries by location zone (wins vs direction_miss):**

| Zone | wins | direction_miss | win share | n |
|---|---|---|---|---|
| **mid_range** (no level near) | 10 | 9 | **53%** | 19 |
| below_PDL | 4 | 3 | 57% | 7 |
| **above_PDH** (broke high) | 1 | 3 | **25%** | 4 |
| near_PDH | 2 | 1 | 67% | 3 |
| near_PDL | 0 | 2 | 0% | 2 |

**Findings (honest):**
1. **The strategy is location-blind.** Half of all trades (19/35) fire in **mid_range** — no meaningful level nearby — and those are a **53% coin-flip**. That *is* the direction problem: entries are momentum-chases with no location context.
2. **The article's naive "break → buy" is REFUTED here.** `above_PDH` entries (price already past yesterday's high — breakout chasing, all CE) won only **1 of 4** — the breakouts *faded*. E.g. 06-05 bought CE at 54923/54912, ~170 pts **above** PDH 54750 → both lost. Chasing the extended breakout is a trap, not an edge.
3. **The unifying pattern is *extension-chasing*:** losers cluster where price is extended in the trade's direction — CE bought above/into resistance (06-03/04/05), PE sold below support (06-01/02, `below_PDL`). The strategy enters *with* an extended move right as it reverts.
4. **Level-proximate setups are too few to judge** (near_PDH/PDL = 2–3 trades). Can't yet prove "only trade at levels" helps — we simply don't take those setups today.

**Verdict on the article:** the framing is right — *location is a real, fully-available signal the strategy ignores* — but the prescription must be **location-AWARE, not breakout-buying**: prefer level-proximity, and **fade/avoid extensions** (don't buy CE far above PDH, don't buy PE far below PDL). Naive "buy the break" would have lost. Needs more days + a CE/PE direction split before building.

## What-if P&L backtest of the location filters — REFUTED

Before building a `LocationSense` gate, backtested the filters by removing the vetoed trades and recomputing P&L per day.

| Day | base | v1 anti-ext (drop CE@above_PDH, PE@below_PDL) | v3 levels-only (drop mid_range) |
|---|---|---|---|
| 06-01 | +6.29 | **0.00** (removed all 4) | +6.29 |
| 06-02 | +5.31 | +2.87 | +0.11 |
| 06-03 | +7.22 | **+13.72** (removed the −6.5% loser) | −6.48 |
| 06-04 | −3.97 | −3.97 | −0.39 |
| 06-05 | −4.72 | **0.00** (removed all 3) | −4.72 |
| **Total** | **+10.13** | **+12.62** | **−5.19** |

**The location filter does NOT hold up:**
1. **v1 anti-extension nets +2.5% but it's noise, not edge.** It *helps* on 06-03/06-05 (removes losers) and *hurts* on 06-01/06-02 (removes **winners**) — the per-day deltas are large and sign-inconsistent (−8.7 on two days, +11.2 on two days). On 5 days that's a coin-flip, not a robust rule.
2. **The article's own thesis backfires on 06-01:** the four `PE below_PDL` trades (textbook "extension chase," which the filter vetoes) were the day's **winners** (+6.29%). Vetoing them zeroed a green day.
3. **"Only trade at levels" (v3) is actively harmful: −5.19% vs +10.13%.** The mid-range momentum trades — which have no level context — are where the green-day money is. Restricting to levels destroys the edge.
4. **Why the earlier win-rate-by-zone table misled:** tiny samples (`above_PDH` n=4) and P&L driven by a few big trades, not win-rate. A zone can be 25% win-rate yet P&L-positive, or 57% yet a wash.

**Verdict: do NOT build the location gate.** Location is a real, available signal, but as a *hard zone veto* it does not produce a robust P&L edge on this week — and the strict version loses money. The cheap backtest saved us from shipping a filter that would have killed winning days. **Direction remains genuinely hard** (consistent with the whole project arc). Next honest step is **more data** (5 days is too few; these are noise-level swings), then test location as a *continuous feature in a direction model* — not a static rule — or accept the documented structural-CE-bias + abstain fallback.

## Raw data
```json
[{"date":"2026-06-01","trades":4,"wins":2,"pnl_pct":6.286,"tags":{"win":2,"direction_miss":2}},{"date":"2026-06-02","trades":9,"wins":4,"pnl_pct":5.309,"tags":{"win":4,"direction_miss":4,"cost_miss":1}},{"date":"2026-06-03","trades":14,"wins":8,"pnl_pct":7.22,"tags":{"cost_miss":1,"direction_miss":5,"win":8}},{"date":"2026-06-04","trades":7,"wins":2,"pnl_pct":-3.966,"tags":{"direction_miss":5,"win":2}},{"date":"2026-06-05","trades":3,"wins":1,"pnl_pct":-4.719,"tags":{"win":1,"direction_miss":2}}]
```
(Per-trade detail captured in the run log; ZERO exit_miss/entry_miss/noise across all 37 trades.)

# ConfluenceDirector + 5-Gate Pipeline — rigorous design (2026-06-12)

Bench consulted: **architecture, math, science, traders** — fact-checked online + against our own data.

## 0. Evidence base (online + offline, fact-checked)
- **ORB academic/industry lit:** index-futures ORB profits come from **reward:risk + trend-day capture, NOT a high win rate** (win ~40–60%, winners run **2R+**, losers cut at the range edge), and it **requires volume + momentum filters** to cut false breakouts. → *Independently confirms S1's asymmetry design AND the need for a confluence filter.* [ORB backtest refs]
- **Multicollinearity science:** stacking correlated indicators = "**multiple counting of the same information**" → false confidence. **Trend + momentum are the classic collinear pair.** Best practice: **categorize; require agreement across DIFFERENT types.** [StockCharts/Earn2Trade]
- **Our own data:** (a) **agreement-lever** (momentum + OI + max-pain — *independent* families — all agreeing on big moves) = **61% OOS** [[project_direction_lever_2026-06-10]]; (b) per-member analysis: stacking correlated price-momentum was redundant / some **anti-predictive** (vwap the only standalone edge); (c) 5-min direction = 50% coin-flip **outside** a clear regime.
- **ADX:** >25 strong trend, <20 no-trend/range (intraday: noisier, lower or pair with Supertrend). **Supertrend** intraday default (10,3) or (7,2) on 5–15m.

## 1. THE scientific principle (the upgrade over naive confluence)
**Do not count N correlated indicators. Require agreement across K INDEPENDENT factor families.** Independent confirmations multiply (Bayes); correlated ones add ~nothing and breed false confidence.

| Family | What it measures | Signals (mostly have / [compute]) | Independent? |
|---|---|---|---|
| **F1 Trend** | direction & regime (price) | EMA stack, VWAP+slope, [ADX], [Supertrend], structure HH/HL, ORB | primary |
| **F2 Momentum** | rate-of-change (price) | [RSI], [MACD], momentum_15m | ⚠️ **collinear with F1** → light/secondary vote only |
| **F3 Flow** | participation (NOT price) | OI buildup (long/short), PCR + Δ, [volume], [cum-delta] | ✅ independent |
| **F4 Vol/Options** | positioning (NOT price) | max-pain, OI walls, [IV/skew], [India VIX] | ✅ independent |
| **F5 Location** | *where* (not whether) | [CPR/pivots], PDH/PDL/PDC, [Fib], round#, ORB-level | entry/stop, not a dir vote |

**Confluence rule:** trade direction must be confirmed by **F1 (trend) + at least one of {F3 flow, F4 vol/options}** — i.e., price-trend AND an *independent* family. F2 is a tie-breaker, never a primary (avoids the trend+momentum double-count). F5 decides entry location + stop, not direction.

## 2. The MATH
- Each family Fi votes vi ∈ {+1 with dir, −1 against, 0 missing/neutral}, weight wi (F1=1.0, F3=0.8, F4=0.8, F2=0.3).
- **Conviction** C = Σ wi·vi / Σ wi(present). Fire only if C ≥ τ **and** ≥2 *independent* families agree (F1 + (F3 or F4)).
- **Why it beats cost:** confluence raises win-prob p **and** cuts trade count (fewer pass all gates) — both attack `E = p·W − (1−p)·L − cost×N`. With S1 asymmetry (W≈2R, L≈1R), breakeven p ≈ 33%; ORB lit + our 61% lever say independent-confluence p ≈ 50–61% → comfortably positive **if** trade count stays low and cost is real (~1%).
- **Bayes intuition:** P(up | F1∧F3∧F4) ≫ P(up | F1∧F1_momentum∧F1_ema) — independence is the multiplier; redundancy is not.

## 3. THE 5-GATE PIPELINE (order matters — ML is LAST)
```
G1 REGIME      ADX>thr / Supertrend / EMA-stack / ORB → trending? else SIT OUT (or → S3 seller)
G2 DIRECTION   the trend's side (bull/bear) from F1 structure
G3 CONFLUENCE  ConfluenceDirector: F1 + (F3 or F4) agree, conviction C ≥ τ
G4 ML VERIFY   comp_020pct entry model: P(≥0.20%/~110pt move) ≥ thr  ← confirmation, not initiation
G5 ENTER       at an F5 level; stop = level invalidation (wide, structural ~90pt); trail the trend
```
Each gate is a filter ⇒ **few A+ trades/day** ⇒ low cost-drag (the wall). ML is consulted **only** on trades already inside a confirmed trend — exactly where it's reliable (it's false in low-vol/chop).

## 4. TRADER playbook + an open tension to resolve
- Textbook: trade **with** the trend, enter on a **pullback to a level** (CPR/VWAP/Fib) with momentum+volume confirm, stop beyond the level, target **2R+**. Matches ORB lit + S1.
- **⚠️ Tension:** our T4 control (6 June days) showed **chase@confirm BEAT pullback** (+75R vs +50R). Trader-lore says pullback; our small sample says chase. **The SIM must A/B pullback vs chase on the 209 days** — don't assume.

## 5. FALSIFIABLE hypotheses + validation (science discipline)
- **H1:** confluence-gated win% > regime-only win% (does the independent-family filter actually lift p?).
- **H2:** independent-family agreement (F1+F3/F4) > correlated-count (F1+F2 momentum stack). *(Directly tests the multicollinearity thesis.)*
- **H3:** edge survives **drop-top3 + walk-forward OOS** on 209 days (not the 6 June days — overfit risk).
- **Guard:** tune τ/weights on 209 historical, hold out; never fit to June.

## 6. Buildability
- **Have:** EMA9/21, VWAP+price_vs_vwap, OI + Δ(30m), PCR + Δ(5/15/30m), max-pain.
- **Compute (cheap, from fut bars):** ADX, RSI, MACD, Supertrend, ATR, CPR/pivots, Fib, OBV. **Verify in snapshot:** volume, India VIX, IV/skew, OI-wall strikes.
- **New module:** `ConfluenceDirector` (G3) — factor-family votes + conviction; wired between regime (G1/G2) and ML verify (G4), feeding S1 entry/stop/trail (G5).

## 7. SIM plan (next)
On 209 days + June: compare **(a) regime-only**, **(b) +confluence F1+F2 (correlated, control)**, **(c) +confluence F1+F3/F4 (independent, the bet)**, each × {pullback, chase} × the S1 stop/trail — metrics: trades/day, win%, avg-R, drop-top3, and CLEAN option-P&L (the gating number). H1/H2/H3 decided here.

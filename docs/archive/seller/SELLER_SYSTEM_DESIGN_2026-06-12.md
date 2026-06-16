# S3 Seller System — one clean setup (trader × quant × architect, 2026-06-12)

Cross-disciplinary design pass, fact-checked online. Goal: **one clean, unified setup** —
not a pile of strategies — that harvests the volatility-risk-premium with discipline.

## Online evidence that shapes it
- **IV gate:** sell when **IV-rank ≥ 30%** (or credit ≥ 25–33% of width); avoid low-IV (credit/width < 20%) — premium too thin. [journalplus, menthorq]
- **Regime → structure (validated):** **trending → vertical credit spread; range-bound → iron condor.** Credit spread wins in trend years (2017/19/23); condor wins in chop years (2015/18/22). Condor collects ~2× credit on the **same** margin when there's no directional view. [apexvol, optionalpha, PL Capital]
- **DTE / theta:** 30–45 DTE = theta sweet spot (decay without explosive gamma); avoid the **expiry-week gamma**. 0DTE only for calm/range + intraday-managed. [tradingblock, schwab]
- **Management:** take profit at **50%**, stop, time-exit (Tastytrade 4k-trade study). [advancedautotrades]
- ⚠️ **BankNifty is MONTHLY-expiry now** (weeklies removed late-2024) — verify; use the **nearest liquid monthly**, hold multi-day for theta, exit before the expiry-week gamma.

## 🎤 The three experts converge

**TRADER —** "Let the *regime* pick the structure, sell only when premium's rich, keep risk defined, manage at 50%, and don't fiddle."
- Regime → BULL-PUT (up) / BEAR-CALL (down) / IRON-CONDOR (range) / SIT-OUT (chop).
- IV-rank ≥ 30 gate. ATM short, 300 wide, **depth-liquid legs only** (cut slippage).
- Hold multi-day for theta; exit at 50% TP / 2× stop / before expiry gamma. **No rolling in v1.**

**QUANT —** "The edge is structural, the risk is bounded, the sizing is humble."
- Edge = **VRP (IV > realized vol)** + **directional timing** (regime picks the side; selling tolerates ~55% direction because the win-zone includes 'flat').
- Defined-risk spread ⇒ tail is **capped** (₹6k), so multi-day holding adds theta without adding tail.
- Expectancy `E = p·(credit harvested) − (1−p)·(managed loss) − cost`; with p≈60%, managed at 50%/2×, and IV-gated, E>0 **conditional on regime**.
- **Sizing: fixed 1 lot** until proven; then scale by fixed-fraction, never martingale.
- **Falsifiable**: must clear drop-top3 + walk-forward OOS + **the live regime on paper** before real money.

**ARCHITECT —** "One brain, one safe executor, one manager — config-driven, paper-mirrors-live."
```
SENSES → BRAIN → EXECUTOR → MANAGER → RISK   (one linear, testable pipeline)
```
- **RegimeClassifier** (have: ADX/EMA/VWAP/ORB + India VIX) → TREND_UP/DOWN, RANGE, CHOP
- **IVGate** (new, cheap: from chain ce_iv/pe_iv → IV-rank) → rich enough?
- **StructureSelector** → vertical | condor | sit-out  (pure function of regime+IV)
- **StrikeSelector** (have: chain + depth) → ATM short, width, depth-liquid legs
- **MLOverlay** (have: 020 entry model) → *defensive veto* if a big adverse move is likely
- **SafeExecutor** (extend Dhan per-leg layer) → state machine **FLAT→OPENING→OPEN→CLOSING→FLAT**, **buy-protective-leg-first, confirm each fill, unwind-on-fail** → never an orphan; handles 2-leg (vertical) AND 4-leg (condor) with the same logic
- **PositionManager** → 50% TP / 2× stop / time-exit; tracks both legs from the live chain
- **RiskGates** (have) → 1 position, defined ₹6k, daily-loss cap, sit-out filters

## The ONE clean setup (the whole thing on a page)
```
Per evaluation:
  1. RegimeClassifier  → TREND_UP | TREND_DOWN | RANGE | CHOP
  2. IVGate            → IV-rank ≥ 30% AND credit ≥ 25% width ? else SIT OUT
  3. StructureSelector → TREND_UP:bull-put · TREND_DOWN:bear-call · RANGE:iron-condor · CHOP:sit-out
  4. StrikeSelector    → ATM short, 300 wide, only depth-liquid strikes (nearest liquid expiry)
  5. MLOverlay (veto)  → 020 model says big adverse move likely? → skip/widen
  6. SafeExecutor      → buy hedge leg(s) first → confirm → sell short leg(s) → confirm → (fail→unwind→FLAT)
  7. PositionManager   → 50% TP / 2× stop / time-exit (hold multi-day; exit before expiry gamma)
  8. RiskGates         → 1 lot, defined risk, daily cap
Reuses: Dhan per-leg adapter, scrip master, feed, snapshot, engine, risk gates.
```

## Build order (one proven thing at a time, paper-first)
1. **v1:** trend-day **vertical** spread, fixed legs, 50%/stop/time, depth-aware fills — **paper**.
2. Add **IVGate** (sell only rich premium) + the **hold-period** that the SIM picks (intraday vs multi-day).
3. Add **iron-condor on RANGE days** (turns the chop-loss into a money-maker).
4. Prove net-positive after real cost **in the current regime on paper** → then 1-lot real Dhan.
5. Later: IV-adaptive width, leg-rolling — only after the base is proven.

**Unchanged discipline:** defined risk always; paper-mirrors-live; no real money until it clears costs in the live regime; one validated piece at a time.

**Sources:** [IV-rank/DTE — journalplus](https://journalplus.co/strategies/put-credit-spread/) · [0DTE/IV — menthorq](https://menthorq.com/guide/trading-zero-dte-iv-rank-relevance/) · [condor vs spread by regime — apexvol](https://apexvol.com/compare/iron-condor-vs-credit-spread) · [iron condor — optionalpha](https://optionalpha.com/strategies/iron-condor) · [Nifty iron condor — PL Capital](https://www.plindia.com/blogs/iron-condor-strategy/) · [50% management — advancedautotrades](https://advancedautotrades.com/credit-spreads-tips-and-best-practices/)

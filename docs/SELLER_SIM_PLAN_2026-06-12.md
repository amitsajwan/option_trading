# Seller SIM — proper backtest, understand, improve (plan, 2026-06-12)

So far the seller edge (+₹1,692/trade, 78% win) is validated on **209 days of 2024 only — a
high-IV regime**, with totals (not per-trade analysis) and no out-of-sample discipline. This
plan turns that into a rigorous, regime-aware backtest + an honest improvement loop.

## The 4 questions this SIM must answer
1. **Is the edge real or regime-luck?** Does it survive low-IV / choppy / tail regimes, OOS, and drop-outlier?
2. **Where does it come from?** Which conditions (IV-rank, regime, DTE, side) drive the wins — and the big losses?
3. **What's the risk?** Max drawdown, max consecutive losses, tail-day loss, Sharpe — not just average.
4. **Can we improve it** without overfitting (walk-forward validated)?

---
## Phase 0 — Data audit + a proper harness (foundation)
- **D0.1 Data audit:** how many chain-days do we actually have, and across which regimes? (Suspect: all 2024 high-IV + ~10 recent low-IV.) Check if 2020–2024 full-chain data exists (GCS/parquet) — **regime diversity is the #1 limitation.**
- **D0.2 Backtest harness:** one driver that runs the *real seller modules* over a date range and emits a **per-trade LEDGER** (entry ts, structure, regime, IV-rank, legs, credit, max_risk, exit reason, days_held, ₹ P&L, **R-multiple** = P&L/max_risk) + an **equity curve**. Everything downstream slices this ledger.

## Phase 1 — Rigorous baseline metrics
Run the validated config (condor, ATM±200, IV≥30, 50%TP/2×, 5d, intraday-monitored) and report:
- win% · avg win · avg loss · **profit factor** · **expectancy** · **Sharpe/Sortino** (daily) · **max drawdown** · **max consecutive losses** · drop-top3/5 · % days in market · monthly P&L.

## Phase 2 — UNDERSTAND (the analysis — the real point)
- **Regime stratification:** P&L by **IV-rank bucket** (low/mid/high), trend/range/chop, VIX, **DTE / expiry-week**, day-of-week. → *Where is the edge concentrated? (hypothesis: high-IV only.)*
- **Winner vs loser profile:** what distinguishes them at entry (IV, regime, OTM distance, which side breached)?
- **Loser autopsy:** the worst trades — gaps? expiry-week gamma? vol spikes? a specific avoidable condition?
- **The IV-dependence test:** re-score on the recent low-IV days — does the edge hold or vanish? (This is the T9 question, in sim.)

## Phase 3 — IMPROVE (iterate, walk-forward validated — no in-sample overfit)
Levers to sweep, each judged on **OOS** + drop-outlier:
- **Strike:** fixed offset → **delta-based** short (e.g., 15–20Δ); width.
- **Gate:** IV-rank threshold; add a **realized-vs-implied (VRP) filter** (sell only when IV ≫ recent RV).
- **Management:** TP% (40/50/60), stop (1.5×/2×), DTE-exit, max-hold.
- **Regime filters:** skip the loser conditions found in Phase 2 (e.g., pre-event, expiry-week, trend-breakout against the short side).
- **Sizing:** flat 1-lot vs IV-rank-scaled (sell bigger when premium richest) — only after edge proven.
- **Structure:** condor vs vertical-on-trend split, re-tuned.

## Phase 4 — Validate the improved config
- **Walk-forward** (rolling train→test) + **time-split OOS** + **drop-outlier**.
- **Stress test** on known tail days (gap/vol-spike) — confirm defined-risk floor holds, quantify worst case.
- Feed the winner into the **live paper daemon (T9)** for the live-regime confirmation.

## Phase 5 — Decision
Go-live (1 lot) only if the improved config is **net-positive after real cost across regimes, OOS, drop-outlier-safe, with acceptable drawdown**, AND T9 live paper confirms in the current regime.

---
## Execution order (what I'll build first)
1. **Phase 0 harness** — the per-trade ledger backtest (highest value; everything depends on it).
2. **Phase 1 + 2** — baseline metrics + regime stratification (the "understand").
3. Decide improvements from the analysis, then **Phase 3** walk-forward.

## Discipline (unchanged)
- Per-trade ledger, not totals. · OOS + walk-forward, never fit-to-history. · drop-outlier on everything.
- Real money stays OFF until Phase 5 + T9 both pass. · Defined risk always.

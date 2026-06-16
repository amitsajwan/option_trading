# Gate Forensics & Evidence-Based Configuration

*2026-06-14. Forensic verification of each entry gate, grounded in (a) a fresh
causal run of the opportunity scorer over 7 real days, and (b) the prior
permutation/combination tests on direction and exits. Goal: get every gate
"properly in place" with its role, threshold, and the evidence behind it.*

---

## 0. The reframing (proven)

The old Gate-1 (`atr_ratio ≥ 0.00088`) is an **absolute elimination filter**.
Forensic proof, in-window (09:45–14:30) bars that cleared it:

| Day | vol-gate bars (≥0.00088) | opportunity-gate trades |
|---|---:|---:|
| 2026-05-26 (dead) | 0 | **0** (cost floor protects) |
| 2026-05-27 | 3 | 3 |
| 2026-06-01 | 32 | 3 |
| 2026-06-02 | 74 | 3 |
| 2026-06-10 (quiet) | **0** | **2** |
| 2026-06-11 (quiet) | **0** | **3** |
| 2026-06-12 (quiet) | **0** | **3** |

The absolute gate goes **structurally dark on quiet days** (0 trades on 3 of 7
days) yet fires 32–74 times on busy days. The opportunity gate is **active on
quiet days and disciplined on busy days** — exactly the trader behaviour we want.

---

## 1. Gate-by-gate configuration

### Gate 1 — OPPORTUNITY (was: vol elimination)
**Role:** rank every bar by a session-relative score; surface the day's best.
**Evidence-based config:**
```yaml
opportunity:
  warmup_bars: 15
  weights: { atr_percentile: 0.40, atr_acceleration: 0.25,
             volume_percentile: 0.20, straddle_expansion: 0.15,
             regime_quality: 0.0 }   # regime_quality pending live wiring
  selection: { mode: percentile, percentile: 85 }   # see §2
```
- Causal session-relative percentile (no full-day lookahead).
- **Finding:** percentile 70→90 gives the *same total trades* (17) — because the
  **daily budget is the binding constraint**, not the percentile. Percentile tunes
  *which/quality*, budget tunes *quantity*. Use **85** (it surfaces afternoon
  opportunities the morning would otherwise crowd out — see §2).

### Gate 2 — COST FLOOR (the one absolute gate)
**Role:** economic viability — abstain if the move can't pay for itself.
```yaml
cost_floor: { min_expected_move_pts: 108, hold_bars: 10 }
```
- Expected move = `atr_14_1m × √hold_bars` (horizon-matched). **Not** the ATM
  straddle (that's expected move to *expiry*, days — wrong horizon).
- **Validated:** dead day 2026-05-26 → 0 trades (peaks below 108 pts). June 12
  13:41 → 112 pts > 108 → viable (barely). This is the principled replacement for
  the magic `0.00088`.

### Gate 3 — REGIME
**Role:** route to the right exit stack (TRENDING/BREAKOUT → lottery; rest →
scalper) and supply `regime_quality` to the opportunity score. Keep as-is;
adaptive exit is **confirmed best** by the prior exit sweeps.

### Gate 4 — DIRECTION (was: veto → now: SIZING)
**Role:** decide *how* to trade, not *whether*. Justified by the
permutation/combination tests:
- Per-member accuracy over 37k 2024 bars: `atm_oi` 52%, `max_pain` 51% (mild +);
  `momentum_15m` 48% and `vwap` 50.5% (**anti**/noise); quorum 50.3% = coin-flip.
- 2026 OOS quorum 43.9% → **inverts**. The 3-way agreement "lever" hit 61% on big
  moves in *both 2024 halves* but does **not** survive into 2026.
- **Conclusion:** direction is weak and non-stationary. So:
```yaml
direction:
  members: { oi: keep, max_pain: keep, ema: keep, momentum_15m: DROP, vwap: 0.5 }
  conviction_strong_margin: 0.60   # ≥ → small directional tilt; else straddle
trade_selector:
  strong_bull: buy_ce
  strong_bear: buy_pe
  weak:        straddle            # default — we usually can't pick a side
```
Default to **straddle** (direction-agnostic) because the volatility edge is real
and the direction edge is ~coin-flip. Drop `momentum_15m` (anti-signal).

### Gate 5 — DAILY BUDGET (required once you rank)
```yaml
budget: { max_entries_per_day: 3, min_spacing_minutes: 20 }
```
- **Finding:** this is the binding constraint (caps every busy/quiet day at 3 →
  ~2.8 trades/day across the 7 days). With 1-lot / ₹41k real sizing, that's the
  right cadence.

---

## 2. The one real defect found: early-session selection bias

Budget is greedy + first-come, and **early bars rank against a thin
distribution**, so they easily hit the 100th percentile and consume the budget
before the afternoon. June 12: 13:41 (a real live entry, score 86, viable) was
**selected but squeezed out** by morning trades (09:48/10:09/10:48).

Mitigations tested:
- **percentile 85–90** → June 12 final set shifts to include 13:32 (afternoon).
- **delay first trade to ~10:00** (session maturity) → June 12 → 10:09/10:48/13:32.

**Principled fix — implemented & VALIDATED:** rank components against a **multi-day
rolling baseline** (trailing ~3 sessions) and select on an **absolute score cutoff**
(`selection_mode="score_cutoff"`) instead of today-only percentile. Early-session
bars now rank against stable recent history, not a thin morning.

Result on the same data (cutoff sweep, 3-day baseline, budget 3 / spacing 20):

| Day | cutoff 65 | cutoff 70 |
|---|---|---|
| 2026-06-12 | `09:48, ` **`13:41`** | `09:48, ` **`13:41`** `, 14:15` |
| 2026-06-10 | `11:20` | `11:23` |
| 2026-06-11 | `09:47, 10:09` | `09:47, 12:30` |

**13:41 — the actual live entry — is now selected** (score 75), and cutoff 70 also
captures the afternoon vol-expansion (14:15, score 83, the day's best). Scores sit
on a stable 60–83 scale, so the cutoff is an interpretable knob. **Recommended:
`selection_mode=score_cutoff`, `score_cutoff≈70`, 3-day baseline** (locked by tests
`test_score_cutoff_*`). This replaces tuning `0.00088` with tuning "how good vs
recent days," which is the right, regime-adaptive abstraction.

---

## 3. What is and isn't proven

**Proven (this forensic):** the opportunity gate's *selection behaviour* is
correct — active on quiet days, disciplined on busy days, cost-floor abstains on
dead days, and it would surface the live-traded bars.

**NOT yet proven:** P&L. Selection ≠ profit. The direction tests warn that even
good selection can't beat coin-flip direction + ~1% cost on the BUY side — which
is precisely why the **straddle (direction-agnostic) default** matters. Net edge
must be measured end-to-end with exits.

---

## 4. Next steps (in order)

1. **Multi-day rolling baseline** in the scorer (fix §2 early-session bias).
2. **Wire opportunity gate into the engine** behind `OPPORTUNITY_GATE_ENABLED=0`
   (swap-in for VOL_GATE_ENTRY; same downstream pipeline).
3. **Sim A/B with P&L + exits** (now that the dashboard runs live's engine): opp
   gate vs vol-gate over all 7 days, drop-outlier.
4. **Straddle execution path** (two-leg) for the `weak` selector — largest piece.
5. Promote only on robust, drop-outlier positive P&L. Real money stays on the
   current config until then.

*Data: `c:/tmp/forensic_bars.json` (10 days from mongo), harness
`c:/tmp/forensic_opportunity.py`. Scorer: `strategy_app/engines/opportunity.py`.*

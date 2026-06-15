# Opportunity Gate — score / rank / select (replaces the ATR elimination gate)

*Status: SPEC (2026-06-14). Motivated by the June 12 finding: the ATR gate
`atr_ratio >= 0.00088` is an absolute **elimination filter**, not an opportunity
detector. On June 12 only 16/375 bars cleared it, all in the 09:15–09:26 open
(before the 09:45 window) → structurally zero trades. A fixed volatility
threshold assumes we know, in advance, the volatility level that matters on every
future day. Markets are non-stationary; that assumption is false.*

## Core shift

| | Today (elimination) | Proposed (selection) |
|---|---|---|
| Question | "is `atr_ratio ≥ X`?" | "which bars are the **best** today?" |
| Nature | absolute, binary | **relative, ranked** |
| Failure mode | 0 trades on quiet days; over-trades on wild days | adapts to the day |
| Direction's role | binary veto (trade / no-trade) | **how to trade** (CE / PE / straddle) |

## Pipeline

```
TIME → OPPORTUNITY (score + cost-floor + budget) → REGIME → DIRECTION → TRADE_SELECTOR → RISK
```

`OPPORTUNITY` replaces the binary vol-gate. It produces a candidate when a bar is
*relatively* good **and** economically viable **and** within the daily budget.

## 1. Opportunity score (0–100), causal & session-relative

A weighted blend of **causal percentile ranks within the session so far** (never
full-day lookahead):

```yaml
opportunity_score:
  atr_percentile:      0.35   # rank of atr_ratio within today-so-far
  atr_acceleration:    0.20   # rank of d(atr) over last k bars
  volume_percentile:   0.15   # rank of volume within today-so-far
  straddle_expansion:  0.15   # rank of ATM-straddle premium expansion
  regime_quality:      0.15   # RegimeDirector quality 0..1 → 0..100
```

`score = Σ wᵢ · componentᵢ` → 0–100. Weights sum to 1.0; live-tunable via the
config registry. **Warmup**: the first `warmup_bars` of the session are not
selectable (too few samples to rank against).

## 2. Selection — relative, not a fixed cutoff

A bar is *selectable* when its score sits in the **top (100 − P)%** of the
session's scores **seen so far** (causal expanding percentile), e.g. `P = 80` →
top 20% of the day. This auto-adapts: quiet day → still surfaces the relative
peaks; wild day → naturally more selective. **Do not tune `0.00088`/`0.0006`
anymore — tune `P` (how selective) and the weights (what "good" means).**

## 3. Cost floor — economic, not statistical (the one absolute gate)

Selection says "best *relative* to today"; the floor says "but only if it can pay
for itself." Derived, not hardcoded:

```
expected_move_pts ≥ round_trip_cost_pts
```
- `expected_move_pts`: estimated from the **ATM straddle premium** (the market's
  own expected-move estimate over the hold horizon), falling back to
  `atr_14 · √(hold_bars)` if straddle data is missing.
- `round_trip_cost_pts`: `cost_pct · spot` (cost_pct ≈ 1% per our fill data ⇒
  ~108 pts at 54k). This is the principled replacement for the magic ATR number.

If `expected_move_pts < floor` → **NO TRADE**, regardless of score.

## 4. Daily budget — required once you rank

```yaml
budget:
  max_entries_per_day: 3
  min_spacing_minutes: 20
```
Without this, "take the top bars" becomes "take 20 trades." With 1-lot / ₹41k
real sizing, quality over quantity is the whole game.

## 5. Direction = how to trade, not whether (major shift)

Our evidence: **volatility edge is real, direction edge is ~coin-flip.** So
direction must not gate the trade — it *shapes* it:

```yaml
trade_selector:
  strong_bull:   { strategy: buy_ce }
  strong_bear:   { strategy: buy_pe }
  weak_direction:{ strategy: straddle }   # capture the move without picking a side
```

`strong_*` vs `weak` is set by a direction-conviction threshold. **Straddle is a
new two-leg execution path** — phased (see below), not in the first cut.

## 6. Full proposed config (registry-backed)

```yaml
opportunity:
  enabled: true
  warmup_bars: 15
  weights: { atr_percentile: 0.35, atr_acceleration: 0.20, volume_percentile: 0.15,
             straddle_expansion: 0.15, regime_quality: 0.15 }
  selection: { mode: percentile, percentile: 80 }
  cost_floor: { cost_pct: 0.01, hold_bars: 10, use_straddle_estimate: true }
  budget: { max_entries_per_day: 3, min_spacing_minutes: 20 }
direction:
  conviction_strong_margin: 0.60   # ≥ → directional; below → straddle
trade_selector:
  strong: directional   # buy_ce / buy_pe
  weak:   straddle
```

These become registry keys (`opportunity.*`, `trade_selector.*`) so they live in
`ops/strategy_config.yml` and are SIM-overridable — same single-source machinery
as the rest of the config.

## 7. Build phases (real-money system — flag-gated, sim-validated first)

1. **Scorer module** — pure, causal, param-driven (`opportunity.py`) + unit tests.
   No engine wiring. Lets us compute scores on real days offline.
2. **Sim A/B** — run the scorer over June 12 + a trending day + a wild day on the
   now-fixed sim; compare its selected bars vs the vol-gate and vs live's actual
   entries. Confirm it would have surfaced 13:41/13:56.
3. **Engine wiring behind `OPPORTUNITY_GATE_ENABLED=0`** — swap-in alternative to
   `VOL_GATE_ENTRY`, same downstream pipeline (direction/strike/risk). Default off.
4. **Direction-as-sizing** — wire conviction → CE/PE selection (still no straddle).
5. **Straddle execution** — add the two-leg path to the trade-selector (reuses
   seller-system atomic-or-unwind plumbing). Largest piece; last.
6. **Promote** only after sim shows positive, robust, drop-outlier P&L. Real
   money stays on the current gate until then.

## 8. Why this is the right abstraction

A fixed ATR threshold encodes "I know tomorrow's relevant volatility today."
June 12 disproved that. Ranking measures opportunity **relative to today's
market**, the cost floor enforces **economic viability**, and the budget enforces
**discipline** — which is how a human trader actually operates.

# Compression / State Engine — Plan, Critique & Validation Charter

> Branch: `feat/compression-state-engine`. Written before any code, on purpose.
> This is the charter: what we're testing, how we'll know it's real, and every
> objection raised by the "team" up front. **Validation gates everything — we build
> the live engine only if the harness says the edge is real.**

---

## 0. TL;DR — the hypothesis in one line

> **Big moves are born in compression, not in a big candle.** Detect compression
> (energy building), wait for a breakout that *holds* (acceptance), then ride the
> expansion — and judge direction only on that clean subset.

This reframes our entry from "fire when ATR is already high" (backwards — it fires on
the move's *middle* or the open spike, and goes dark on quiet days) to "fire when ATR
is *low and contracting*, then on acceptance."

---

## 1. What is this new analysis? (plain)

A market-state cycle, classified every minute:

```
DEAD → COMPRESSION → BREAKOUT-SETUP → EXPANSION → EXHAUSTION → REVERSAL → (DEAD)
```

- **Compression** = Bollinger width ↓, ATR ↓, 5–10 overlapping candles, EMA20 flat,
  no candle exceeding the prior range. Energy storing. *Do not trade yet.*
- **Acceptance** = price breaks the range, **closes** beyond it, **does not return
  inside** on the retest, EMA slope turns. *This* is the trigger (not the break candle).
- **Expansion** = ATR ↑, bands widen, EMA9/20/50 spacing grows = trend strength.
- **Exhaustion** = big candles, rising wicks, weak follow-through, ATR peaks → stop
  opening, prepare exits.
- **Direction** = bull/bear confluence score (structure + EMA alignment + momentum +
  acceptance), evaluated **only on the compression→acceptance subset**.

### What's genuinely NEW vs what we already have
| Piece | New? | Note |
|---|---|---|
| Compression-as-trigger | **YES** | Our ATR gate is the *opposite* (fires on high ATR). This is the core bet. |
| Explicit 6-state machine | **YES** | Cleaner than our trend/range/chop regime. |
| Acceptance/retest-hold gate | **YES** | We proved raw ORB is anti-predictive (43.5%); acceptance is the fix. |
| EMA-spacing / exhaustion | partial | New as features; informs exits. |
| Bull/bear confluence score | **NO** | ≈ our existing council. Known ceiling ~55–62% on big moves, non-stationary. |

---

## 2. Data we have (can we build it on our snapshots? — YES)

- `snapshots_ml_flat_v2` — **1199 days (2020 → 2024-10), per-minute**, on the dev box +
  ML VM + GCS. Has per-bar `px_fut_open/high/low/close`, OI totals, PCR, IV, `vwap_fut`,
  `osc_atr_ratio` / `atr_14_1m`, ema slopes, ranges.
- `stage1_entry_view_v3` — 57 engineered features (rsi, ema_9/21/50_slope, atr, vix…).
- **June 2026 forward** — in mongo (`phase1_market_snapshots`), exported per-day (our
  true OOS / current-regime check).

**Computable compression features from OHLC (all derivable, no new ingestion):**
Bollinger width (rolling std of close × k / mid), ATR & its trend, candle range &
overlap (how much each bar overlaps the prior), EMA9/20/50 + their spacing & slope,
range-contraction (range_10 / range_30). **Conclusion: we can build and validate the
entire compression/state engine on existing data — no new pipeline.**

---

## 3. The validation harness — experiments, null hypotheses, gates

Each experiment has a **null** (what "no edge" looks like) and a **pass gate**. Run
**walk-forward** (train/derive thresholds on 2020–2023, test on 2024) + **per-quarter** +
**forward on 2026**. Cost-aware throughout.

| # | Experiment | Null hypothesis | Pass gate |
|---|---|---|---|
| **E1** | **Compression → big move.** Does a compression state precede a ≥X-pt move in N min more than base? | compression bars move no more than random bars | hit-rate ≥ **1.5×** base, stable across quarters |
| **E2** | **Acceptance vs raw breakout.** Does close-beyond + retest-hold beat the raw break? | acceptance = breakout (no improvement) | fake-out rate ↓ materially **and** forward-move ↑ |
| **E3** | **Direction on the subset.** On compression→acceptance setups, does bull/bear confluence beat coin flip / our ~56% baseline? | ~50% (coin flip) | **≥ 60%** on the subset, **both 2024 halves** |
| **E4** | **Cost-aware P&L.** Does the full setup clear ~108pt round-trip cost? | net ≤ 0 after cost | **net > 0/trade after 1% cost, drop-top-2% still ≥ 0** |
| **E5** | **Regime stability.** Does it survive walk-forward + hold on 2026 forward? | inverts/decays OOS | sign-stable 2024→2026, no quarter < 0 |

**Decision rule:** build the live engine **only if E1+E2 pass AND (E3 or E4) pass AND E5
holds.** Direction (E3) is allowed to be the weak link — if E1/E2/E4 pass but E3 is
coin-flip, we ship compression-entry + **straddle** (non-directional) and keep direction
in shadow. If E1 fails, the whole thesis is dead — stop.

---

## 4. The "team" — brainstorm & critique

### 🧭 Product Owner
- **Value:** fixes the quiet-day-0-trades problem *and* improves entry quality — two real
  pains in one. Fits cleanly behind the existing Selection Gate 1 (compression score
  becomes the move-detector the gate ranks).
- **MVP / DoD:** Phase 0 = the harness + a go/no-go verdict. Phase 1 = a `state.py`
  module + `compression_score`, shadow-only. **Definition of Done for Phase 0:** the E1–E5
  table filled with real numbers + a written verdict. No live wiring until then.
- **Scope discipline:** do **not** rebuild the whole engine. Compression is a *better
  move-detector*; the council, selection gate, exits stay.

### 💼 Business
- **Cost/benefit:** Phase 0 is ~days of compute on data we already have — cheap. Payoff
  if real: a tradeable entry on quiet days (today = 0). Risk if we skip validation: another
  in-sample illusion (we've burned weeks on those — see §5).
- **Opportunity cost:** the **seller (premium-selling)** path is still our only *proven*
  +EV line. This must not stall it. Verdict: run Phase 0 in parallel; it's data work, not
  capital.
- **Real money:** unchanged — **OFF** until E3/E4 survive *forward*, not just backtest.

### 📈 Trader (desk view)
- Agrees with the thesis — *this is how moves actually happen.* But:
  - **Fake breakouts dominate** the open and lunch. Acceptance (E2) must be strict —
    close beyond + a *held* retest, not a 1-tick poke.
  - **News/event days** break the cycle (gap-and-go, no compression). Keep the
    event-day/regime guard.
  - **Expiry days** compress IV not price — compression score must use *price* structure,
    not IV alone.
  - **The hardest day is the "tried to trend then came back to mixed"** day (exactly our
    June chart) — direction there is a trap. The state machine should tag it EXHAUSTION/
    REVERSAL and **stand down**, not flip.
- Trader's one ask: **measure the fake-out rate explicitly** (E2). A compression engine
  that buys every squeeze is a chop-machine.

### 📐 Mathematician / Statistician
- **Define the null precisely** and beat it — not "looks predictive." Base rate of a
  ≥X-pt move per bar must be the benchmark for E1.
- **Multiple testing:** we'll try several thresholds (BB k, ATR window, X-pt, N-min).
  That's a p-hacking surface. **Fix thresholds on 2020–2023, freeze, then test 2024 once.**
  Report the *frozen* config's 2024 number, not the best of many.
- **Sample size:** compression→acceptance setups are *rare* by design. Count them. If 2024
  has < ~100 setups, the win-rate CI is wide — say so; don't over-claim on n=20.
- **Look-ahead is the #1 enemy** (it fooled us on rolling velocity, +0.10 AUC of pure
  leak). Every feature at bar *t* uses only bars ≤ *t*, completed bars only. Acceptance
  "retest held" must be evaluated causally.
- **Non-stationarity:** direction inverts by regime. E5 (forward 2026) is not optional.

### 🔬 Scientist (ML/quant)
- Keep it **interpretable first** (rule-based compression score), *then* optionally let an
  ML model learn the state — a transparent baseline we can trust, before a black box.
- **Falsifiability:** E1 is the killer test. If compression doesn't precede moves above
  base rate, the elegant cycle is just a story. We *want* to be able to disprove it fast.
- **Leakage audits:** label = forward move (uses future) — fine for the label, never for a
  feature. Build the label separately; never let it touch the feature row.
- Reuse the harness pattern that already caught the leak: in-process replay over the flat
  parquet, walk-forward split, per-quarter + forward.

### 🧨 Chief Skeptic (the project's scars)
Read these before believing any green number:
- **"+271% net" was +87% in-sample** — the model recognising dates it trained on.
- **Rolling velocity "0.76→0.85"** was almost entirely **15-min look-ahead**; the honest
  number was ~flat.
- **Move-detection ≈ ATR** (corr 0.92) — a fancy detector that doesn't beat a one-liner.
- **Direction is non-stationary** — 50.3% over 37k bars; the 2026 quorum *inverted* (43.9%).
- **Every prior "breakthrough" died on walk-forward or cost.**
- **Therefore:** compression is *promising* and matches our `loaded` finding (49% vs 32%
  for ≥100pt) — but it must clear **E1–E5 with frozen thresholds, OOS, after cost, forward**.
  Until then it's a hypothesis, not an edge.

---

## 5. Risks & guardrails (the traps, and how we avoid them)
| Trap (we've hit it) | Guardrail |
|---|---|
| Look-ahead leakage | completed bars only; label built separately; causal acceptance |
| In-sample / p-hacking | freeze thresholds on 2020–2023, test 2024 **once**; report frozen config |
| Outlier-driven P&L | always report drop-top-2% net |
| Non-stationary direction | E5 forward-2026 mandatory; sign-stability gate |
| Cost ignored | every P&L net of ~1% (≈108pt); never gross |
| Tiny-sample over-claim | report setup count + CI; no verdict on n<~100 |
| Rebuilding the world | compression = a *move-detector* behind the existing gate; nothing else changes |

---

## 6. Phased plan & decision gates
- **Phase 0 — Validation harness (THIS first).** Compute compression score + state per bar
  over 2020–2024 flat parquet; run E1–E5; fill the table; **written go/no-go verdict.**
  *Gate: build Phase 1 only if E1+E2 pass and (E3 or E4) and E5.*
- **Phase 1 — `state.py` module** (compression_score + 6-state classifier + acceptance),
  pure/causal, unit-tested. Shadow-only; wired as the move-detector feeding Selection Gate 1.
- **Phase 2 — Direction on the subset** (bull/bear confluence, abstain off-subset);
  straddle fallback if E3 is coin-flip.
- **Phase 3 — Exits** from expansion/exhaustion (fix MFE-giveback).
- **Phase 4 — Shadow live**, then size-tiny only after forward survival.

---

## 6b. RESULTS — E1 (2026-06-16, harness v1, 1199 days 2020-2024)

| | ≥100pt lift | ≥150pt lift | direction |
|---|---|---|---|
| train (2020-2023) | 1.06 | 1.04 | 44.1% |
| **test (2024, OOS)** | **1.26** | **1.31** | **41.3%** |
| 2024 Q1/Q2/Q3/Q4 | 1.22/1.29/1.28/1.31 | 1.22/1.22/1.54/1.47 | 40.9/37.9/40.5/50.6% |

Lift rises over time: 2020 ~0.8 → 2021 ~1.1 → 2023 up to 1.9 → 2024 steady 1.2-1.3 (all
4 quarters > 1.2). ~15.9k setups total (2.6k in 2024) — ample n.

**Verdict:**
- **E1 PARTIAL PASS.** Compression→acceptance *does* precede a ≥100pt move ~30% more than
  base in 2024, **stable across all quarters, and OOS > train (not overfit).** But below the
  1.5× gate — a modest, real move edge.
- **DIRECTION REFUTED, but informative:** the breakout-acceptance *direction* is 41-44%
  (anti-predictive, every regime) → **FADE the breakout (~59%).** Matches our prior
  mean-revert / fade-vwap finding. "Follow the breakout" is dead; "fade the breakout" is the
  candidate edge.
- **Caveat:** harness v1 likely under-measures (forward move from confirm bar misses the
  thrust; "next-bar-holds" may catch exhaustion pokes). E2 refinement could raise the lift
  and clarify whether the fade is real or a measurement artifact.

**Next:** E2 — refine the setup (measure from breakout bar; stricter compression; longer
horizon) AND test the **FADE direction explicitly, cost-aware, walk-forward + 2026 forward**.
If fade-59% survives cost + forward, *that* is the headline, not the follow-breakout.

## 7. First concrete deliverable (next commit)
`research/compression_harness.py` (run on the ML VM / flat parquet) that, per 2024 day:
1. builds causal compression features (BB width, ATR trend, range-contraction, EMA
   spacing/slope, candle overlap);
2. classifies state per bar;
3. flags compression→acceptance setups;
4. measures **E1** (hit-rate of ≥100/150pt move after a compression-acceptance vs base)
   and the **setup count**, walk-forward + per-quarter.

Output = the first row of the §3 table + a one-paragraph honest read. That single number
(E1) tells us whether the whole thesis is alive.

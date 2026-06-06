# Intelligent Brain — Architecture & Handover

**Date:** 2026-06-06 · **Author:** trading + Claude · **Status:** vision + validated foundation, not yet built
**System state:** HALTED (correct). v3 entry deployed but weak; exit floor fixed in-container; direction is the bottleneck.

This document captures (1) what we proved over the last sessions, (2) the core insight that changes the design, and (3) an architecture for an *intelligent brain* — a reasoning system of many checks, not a single algo or model.

---

## 1. Where we are (the honest scorecard)

We tried to make money buying ATM options on a 5-min ML entry signal. We took it apart and learned:

| Component | Verdict | Evidence |
|---|---|---|
| **Entry (ML)** | Real but **weak** | At 95% confidence, only ~17% of picks saw a 100-pt move (vs ~13–16% base). Lab AUC 0.83 did **not** survive live. |
| **Direction** | **The bottleneck** | ~0.59 model. Wrong-side trades lose −7 to −8%, right-side win +3–5% → negative expectancy even with good entries/exits. |
| **Exits** | **Fixed** (mechanically) | Universal `EXIT_MAX_LOSS_PCT` floor + scalper capture: giveback 4→0, but net got *worse* because better exits just took more wrong-direction trades. |
| **Cost** | The hidden killer | ~1.3% round-trip + theta. A 54-pt move (old label) nets ~flat. Training used 6 bps (20× too low). |
| **Horizon** | **The biggest lever** | 5 min was too short. At **10 min**, base rate of a 100-pt move triples (16%→34%), and signals actually work. |

**The two findings that reframe everything:**
1. **Don't predict direction first — detect the *big move* first.** Direction is near-random and the system's hardest problem. But "is a big move loading?" is detectable.
2. **A 10-minute, compression→expansion detector beats the ML model**, on the *same quiet days where ML failed*:

| BigMoveScore ≥3 (10-min) | hit ≥50 pt | hit ≥100 pt | hit ≥200 pt | fires/day |
|---|---|---|---|---|
| **signal** | 86% | **59%** | **14%** | ~8 |
| base rate | 79% | 34% | 6% | — |
| **lift** | 1.1× | **1.8×** | **2.3×** | — |

The lift *grows with move size* — exactly what a real "spring release" detector should do. 50-pt moves are free and worthless; the edge (and the money) is in the 100–200 pt moves, and that's precisely where the detector is strongest.

---

## 2. The core design principle

> **Stop hunting one magic predictor. Build a system of independent "senses," each of which reports structured evidence, and a reasoning layer that decides — like a trader, not a formula.**

Why this, not another model:
- A single model gave us 1.3× and broke on regime/calibration/version-mismatch. It's a black box we can't reason about.
- The market gives *many* weak, independent signals (compression, flow, regime, time, structure). The edge is in **requiring several to agree**, not in one being strong.
- Everything must be **inspectable**: every trade should come with a sentence of *why*, and every "no-trade" too. That's how we learn and how we trust it with money.

---

## 3. Architecture — three layers

```
            ┌────────────────────────────────────────────────┐
   slow     │  LAYER 3 — OVERSIGHT BRAIN (LLM, reasoning)     │
 (minutes/  │  audits the day, spots regime shifts, tunes      │
  session)  │  thresholds, writes the narrative, learns        │
            └───────────────▲────────────────────────────────┘
                            │ reads traces, proposes changes
            ┌───────────────┴────────────────────────────────┐
   fast     │  LAYER 2 — DECISION BRAIN (deterministic)        │
 (per bar,  │  gathers all SENSE verdicts, reasons in context, │
  <1s)      │  decides: TRADE / WAIT / SKIP, side, size        │
            └───────────────▲────────────────────────────────┘
                            │ structured verdicts (not numbers)
   parallel ┌───────────────┴────────────────────────────────┐
   senses   │  LAYER 1 — THE SENSES (checks / functions)       │
 (per bar)  │  Regime · Compression · Expansion · Direction ·  │
            │  Flow/OFI · Cost/EV · Risk · Execution            │
            └─────────────────────────────────────────────────┘
```

**The key idea: latency separates "reflex" from "reasoning."**
- **Per-bar decisions (Layers 1–2) must be deterministic and fast (<1s).** No LLM in the hot path — too slow, too expensive, too non-deterministic for a 1-minute clock.
- **The LLM "brain" lives in Layer 3** — it runs per-session / post-trade / on-regime-change. It does the *meta* reasoning the user is picturing: reviewing what the senses said, catching when the world changed, adjusting the deterministic layers, and explaining decisions in English. That's where "intelligence" compounds — not on every tick.

This is the honest version of "parallel agents + a main brain reasoning": the *senses run in parallel* each bar (cheap pure functions), the *decision brain* synthesizes them fast, and the *oversight brain* (the real reasoning agent) supervises and adapts.

---

## 4. Layer 1 — The Senses (each returns STRUCTURED evidence, never a bare number)

Every check returns a small record: `{verdict, confidence, evidence, value}` — so the decision brain can *reason*, and we can *audit*.

| Sense | Question | Returns (example) | Status |
|---|---|---|---|
| **DayPersonality** | What *kind* of day is this? | `{type: "trend"\|"mean_revert"\|"gap"\|"expiry"\|"news"\|"low_liquidity", confidence}` | partial — extend existing `TraderDayType` (TREND/REVERSAL/BALANCED/NO_TRADE) in `strategy_app/market/trader_judgement.py:13` |
| **IntradayRegime** | Is the market alive *right now*? | `{state: "alive"\|"compressed"\|"expanding"\|"dead"\|"chaotic", reason: "ATR>baseline, not lunch"}` | to build (gate) |
| **Compression** | Is a spring loading? | `{loaded: true, tightness: 0.62, bars_compressed: 12, oi_building: true}` | ✅ proven |
| **Expansion** | Is it releasing *now*? | `{triggered: true, strength: 2.1, via: ["volume","velocity"]}` | ✅ proven |
| **MoveFunction (BigMoveScore)** | Combined: big move coming? | see §5 below | ⚠ top-bucket works, **not monotonic** — see Phase 0 (§8) |
| **Destination** | Is there *room* for the move to run? | `{nearest_support: 53900, nearest_resistance: 54200, available_space_up: 80, available_space_down: 220, expected_move_pt: 135, space_to_move_ratio: 0.59}` | **to build (new — key gap)** · seed from `TraderAnnotationRecord.invalidation_reference` |
| **Direction** | Which way — or *UNKNOWN*? | `{side: "CE"\|"PE"\|"UNKNOWN", confidence: 0.0–1.0\|null, basis:["vwap+","ofi+"]}` — **low-confidence CE = UNKNOWN, not CE** | ⚠ weak; contract needs UNKNOWN (today `direction:str=""` only, `contracts_app/decision_events.py:99`) |
| **Flow/OFI** | Is one side being hit? | `{net_ofi: +0.3, ce_depth>pe_depth: true}` | data ready, didn't help *timing* (may help direction) |
| **Cost/EV** | Does the expected move pay after cost? | `{exp_move_pt: 135, exp_option_gross: 0.067, net_after_cost: +0.04, +ev: true}` | to build → feeds OpportunityQuality (§6) |
| **Risk** | Are we allowed to trade? | `{ok: true, daily_dd: -1%, consec_losses: 1, in_position: false}` | exists (tracker) |
| **Execution** | Can we get filled cleanly? | `{strike: 54200, spread: 0.4%, liquidity: ok}` | partial (depth ticks) |

> Senses stay independent (one job, no peeking). **Comparing senses to each other is a Layer-2 job, not a sense** — see ConflictAnalysis in §6.

**Design rules for senses:**
- One job each, no peeking at others (independence is what makes agreement meaningful).
- Always return *evidence*, so "why" is free.
- Always allow "unclear/abstain" — a sense that isn't sure should say so, not guess.

---

## 5. The "Move function" — what it should return

Not `True/False`, not just a score. A structured verdict the brain can reason over:

```python
MoveVerdict = {
  "score": 3,                       # 0-4 (compression + vol_release + velocity + oi_build)
  "components": {                   # so the brain sees WHY
     "compression": 1, "vol_release": 1, "velocity": 1, "oi_build": 0
  },
  "expected_move_pt": 135,          # avg realised move at this score (from backtest)
  "prob_100pt_10m": 0.59,           # calibrated hit-rate at this score/regime
  "prob_200pt_10m": 0.14,
  "horizon_min": 10,
  "confidence": "high",             # derived from score + regime agreement
}
```

The brain then asks the *other* senses (direction, cost, risk) and decides. The move function says "a 135-pt move is loaded, 59% odds" — it does **not** say "trade." That separation is the whole point.

---

## 6. Layer 2 — The Decision Brain (deterministic, per bar)

Not a fixed `score≥3 → trade`. The brain runs two Layer-2 steps the senses can't do themselves, then applies a policy that *requires agreement*, *abstains when unsure*, and *trades only the highest-quality opportunity*:

**(a) ConflictAnalysis** — compares senses for contradictions (this *must* live here, not in Layer 1, because it peeks at every sense):
```
move_strong_but_direction_conflicted | ofi_bullish_price_falling | velocity_up_volume_weak | loaded_but_no_space
```
Contradictions are often more informative than confirmations — a contradiction forces WAIT/SKIP, never a trade.

**(b) OpportunityQuality** — the final ranking invariant (promoted from a sense to *the* gate). A signal is only worth one of the day's few trades if its net edge clears a threshold:
```
edge = expected_option_profit − spread − slippage − brokerage − theta
quality = rank(edge, prob_200pt, space_to_move_ratio)        # 0..10
```

Policy:
```
IF NOT regime.alive:                      -> NO-TRADE (dead/wrong personality)
ELIF move.score < 3 OR NOT move.released: -> NO-TRADE (no loaded+released spring; see Phase 0)
ELIF conflict.any:                        -> WAIT  (senses disagree)
ELIF direction.side == UNKNOWN:           -> WAIT  (loaded, side unclear — re-check next bars)
ELIF destination.space_to_move_ratio < 1: -> SKIP  (no room: resistance/support too close)
ELIF opportunity.edge <= threshold:       -> SKIP  (not worth a trade slot)
ELSE:                                     -> TRADE side, size = 1 lot (FIXED — always)
ALWAYS:                                   -> write reasoning trace (incl. all sense verdicts)
```

Two non-negotiables baked in here:
- **Trade rarely, only on agreement.** ~8 move-signals/day → maybe 2–4 trades after direction+cost+risk agree. Fewer, better.
- **Every outcome (trade *and* no-trade) writes a reasoning trace.** This is the dataset the (future) oversight layer learns from.

**Size is always 1 lot — there is no sizing decision.** The only risk lever is *selectivity* (trade or don't). `OpportunityQuality` and `prob_200pt` are used to **rank and gate** (take the best, skip the rest), never to size. Asymmetric payoff comes from the *move distribution itself* (the ~14% that reach 200 pt), not from betting bigger.

---

## 7. Layer 3 — The Oversight Brain (OPTIONAL — the human does this today)

**Not required to trade.** The whole trading path (Layers 1–2) is deterministic math and needs no LLM/ML. This layer is a *convenience*: for now **you** are the oversight brain (read traces, adjust by hand or with simple rules). Build an LLM here only if/when hand-reading traces becomes the bottleneck. When it exists, it lives off the hot path, where it can afford to think:

- **Per-session pre-open:** read overnight/regime context, set today's posture ("VIX up, gaps — widen stops, trade only A+ setups").
- **Intra-day, on regime change:** notice the senses disagreeing with outcomes ("compression firing but moves dying — market regime shifted, tighten").
- **Post-trade:** for each trade, audit the trace — did the senses agree? was the loss a direction miss or an exit miss? Tag it.
- **End-of-day:** write the narrative, propose threshold/weight adjustments (never auto-applied to live without a sim gate), update the playbook.
- **Continuous:** maintain the "what works in which regime" memory.

It does **not** place trades. It *supervises and adapts* the deterministic layers, and explains everything in English. Think senior trader reviewing a junior's tape — not the junior's trigger finger.

---

## 8. Integration plan (phases)

1. **Foundations (lock these first):** 10-min horizon everywhere; cost model = 1.3% + theta; "big move first" not "direction first." *These are settled by the data; don't relitigate.*

2. **Phase 0 — BigMoveScore calibration proof (THE GATE — do this before building any brain).**
   For every bar, compute score 0..4 and `max(abs(future_move_pt))` over 10 min. Report **median, p75, p90, and hit-rate for 50/100/200 pt per score bucket.** Require **monotonicity** (bigger score → bigger move) — or explain every non-monotonic bucket — before proceeding.
   **Current status (7 live days, n=2121): NOT yet monotonic.** mean move by exact score: `0→95, 1→90, 2→106, 3→135` (score 4 never occurred). It's flat at the bottom (0≈1≈base) then jumps at 3. **Diagnosis:** compression *alone* is an anti-signal (a quiet bar stays quiet); the move needs **release** on top. **Required fix before the gate passes:** reformulate as `loaded = compression AND oi_build` + `released = velocity AND volume`, **trade only `loaded AND released`** (not "≥3 of 4"), then re-run this proof. **Decision rule: if score 3/4 still does not produce reliably larger 10-min absolute excursions than 0/1/2 — on a larger sample — STOP. Do not build the brain on an uncalibrated detector.**

4. **Phase 1 — Senses as functions.** Implement DayPersonality, IntradayRegime, Compression, Expansion, MoveFunction, **Destination**, Cost/EV, Risk as pure functions returning structured verdicts. Compression/Expansion/Move(top-bucket) already validated; Destination is the new build.
5. **Phase 2 — Decision brain + traces.** Wire the deterministic policy (§6, incl. ConflictAnalysis + OpportunityQuality + Destination gate). Every bar writes a reasoning trace. **Backtest end-to-end on live days: net P&L with direction + 10-min exit + cost.** This is the go/no-go.
6. **Phase 3 — Direction sense (UNKNOWN-first-class).** The bottleneck. Build structural direction (VWAP/OFI/CE-PE depth); **two-stage: side ∈ {CE,PE,UNKNOWN}, confidence 0..1 — a low-confidence CE *is* UNKNOWN.** Extend the contract (`DirectionDecisionEvent`, `contracts_app/decision_events.py:99`, today `direction:str=""` only). A/B vs ML direction; abstain (UNKNOWN→WAIT) rather than trade blind.
7. **Phase 4 — Exit as a sense.** The floor + scalper are in; make exits regime/horizon-aware (10-min hold for a loaded breakout, tight for a fade).
8. **Phase 5 — Oversight (OPTIONAL, deferred).** *You* are the oversight brain for now (read traces, adjust by hand / simple rules). An LLM here is a convenience, **never a dependency** — don't build it until the deterministic system is profitable and hand-reading traces is the bottleneck. The trading path is 100% deterministic and needs no LLM/ML to run.
9. **Phase 6 — Shadow → live.** Paper/shadow for weeks; only size up on proven live edge.

**Gate at every phase:** sim-validate on full raw data before anything touches live. **Phase 0 is the hard gate** — the system stays HALTED until (a) the MoveScore proof calibrates and (b) Phase 2's end-to-end backtest clears cost.

---

## 9. What to keep, what to drop

- **Keep:** BigMoveScore (4 signals, 10-min); the exit floor + scalper; v3 entry as *one input* (it's a fine "is this bar interesting" sense, just not a standalone decision); the full-raw-chain persistence; the sim harness (e2e on unfiltered mongo data).
- **Drop / demote:** the 5-min horizon (dead); depth-OFI as a *timing* signal (didn't help — retry only for *direction*); the idea that any single model is "the entry."
- **Fix:** direction (the only thing that moves PnL now); cost realism in every label/eval (no more 6 bps).

---

## 10. Honest risks & open questions

- **Sample size:** all live findings are 7 quiet days (~2,400 bars). The lifts are real but need OOS / more accumulated live days before real size. Collection started ~late-May-2026; more data accrues daily.
- **Capture ≠ opportunity:** "59% see a 100-pt move" is the *opportunity*; the move can revert. Exits must hold winners (the giveback problem) — Phase 4 must prove this in the e2e backtest, not assume it.
- **Direction may stay hard:** if the structural direction sense also can't beat ~0.55, the honest fallback is the documented structural CE bias + abstain-when-unclear, trading far fewer but cleaner setups.
- **Latency discipline:** keep the LLM out of the per-bar path. If it creeps in, the system stops being tradeable.

---

## 11. One-paragraph summary for whoever picks this up

We stopped trying to predict direction with a model and instead built a **big-move detector** (compression→expansion) on a **10-minute** horizon — and on the same quiet days the ML failed, it nearly doubles the odds of catching a 100-pt move (34%→59%) and triples them for 200-pt moves, ~8 signals/day. The plan is **not** to turn that into a rigid algo, but into a **layered brain**: independent "senses" (regime, compression, expansion, direction, cost, risk) each reporting structured evidence; a fast **decision brain** that trades only when they *agree* and abstains when they don't; and a slower **LLM oversight brain** that audits the day, adapts the thresholds, and explains every call. Direction is the remaining hard problem and the next build. Everything stays halted and sim-gated until the end-to-end, cost-aware backtest clears.

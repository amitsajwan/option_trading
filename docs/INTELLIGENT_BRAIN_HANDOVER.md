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
   **Status (7 live days):**
   - *Sum-of-4 score was NOT monotonic* (`0→95, 1→90, 2→106, 3→135`; score 4 never occurred) — a lone signal is noise.
   - *Reformulation RUN:* split into `loaded = compression AND oi_build` and `released = velocity AND volume`. **`loaded` is a clean, validated signal:** mean move **117 pt, 49% ≥100 pt, 11% ≥200 pt** (n=229, ~33/day) vs base **93 pt / 32% / 5%** → ~1.5×. The core "spring loading" detector **calibrates** (neither 32% → loaded 49%).
   - *Open:* the `released` trigger as defined (velocity AND volume on the **same** bar) **never fired** — too strict. The release/timing refinement needs a looser trigger (velocity **OR** volume, or a 2–3 bar window) to sharpen *when* inside the loaded window to enter.
   **Decision rule:** the `loaded` detector passes the dose-response gate — proceed to build senses around it. But **fix the `release` trigger and re-confirm on more data before sizing any real money.** If a larger sample shows `loaded` no longer beats base, STOP.

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

We stopped trying to predict direction with a model and instead built a **big-move detector** on a **10-minute** horizon. The validated core is **`loaded` = compression AND OI-building** — on the same quiet days the ML failed, loaded bars saw a ≥100-pt move **49%** of the time vs **34%** base (~33/day). The plan is **not** a rigid algo, but a **layered brain**: independent "senses" (day-personality, regime, compression, expansion, destination, direction, cost, risk) each reporting structured evidence; a fast deterministic **decision brain** that trades **1 lot** only when they *agree* (ConflictAnalysis + OpportunityQuality) and abstains when they don't; an **optional/deferred** oversight layer (human for now). Direction is the remaining hard problem, built **after** entry. Everything stays halted and sim-gated until the e2e, cost-aware backtest clears.

---

## 12. Liquidity / Structure sense — scoped addition (2026-06-07)

A review proposed adding a "Market Structure / Liquidity Sense" (PDH/PDL/weekly/session
distances, liquidity sweeps, structure) as the system's "biggest missing component."
**Assessment: the premise is largely already built.** `StructureSense`
([structure.py](../strategy_app/senses/structure.py)) and `DestinationSense`
([destination.py](../strategy_app/senses/destination.py)) already deliver breakout /
fakeout / at_extreme / coiling, EMA-stack trend, prior-day H/L + ORB + OI-wall S/R, and
`space_to_move_ratio`. Structure is already wired into Layer 2 — conflict
`loaded_into_fakeout` ([decision_brain.py:128-132](../strategy_app/brain/decision_brain.py#L128-L132))
and the `W_STRUCT` quality vote ([decision_brain.py:30-35](../strategy_app/brain/decision_brain.py#L30-L35)).
So this is **not** a missing pillar; it's a *sharpening* of two existing senses plus one
genuinely new (and risky) direction hypothesis. Scoped into three tickets:

### 12.1 — Weekly H/L into Destination · GREEN-LIGHT (low risk)
Destination only knows prior-*day* H/L, ORB, and OI walls. Weekly levels are objective
magnets/walls and the data **already exists**: `SnapshotAccessor.week_high` / `week_low`
([snapshot_accessor.py:564-569](../strategy_app/market/snapshot_accessor.py#L564-L569)),
sourced from the same `session_levels` payload as the prior-day levels already in use.
- **Change:** map `week_high`/`week_low` in [snapshot_adapter.py:94-101](../strategy_app/senses/snapshot_adapter.py#L94-L101)
  and add them to the candidate list in [destination.py:28-33](../strategy_app/senses/destination.py#L28-L33).
- **Gate (cheap but required):** confirm the live/sim snapshot producer actually populates
  `session_levels.week_high/low` (the accessor property exists; verify the field is non-null
  on real bars before relying on it). Pure additive S/R — no thesis change.

### 12.2 — Sweep detection on PDH/PDL + explicit `sweep_direction` · GREEN-LIGHT (low risk)
Today `struct_fakeout` (the "swept a level then snapped back" trap) is derived **only from
the opening range**, never prior-day extremes
([snapshot_adapter.py:43-45](../strategy_app/senses/snapshot_adapter.py#L43-L45)). So
"swept PDH then rejected" is currently invisible.
- **Change:** in `_structure_from_snapshot` ([snapshot_adapter.py:23-59](../strategy_app/senses/snapshot_adapter.py#L23-L59)),
  also flag a sweep when price pierced `prev_day_high`/`prev_day_low` intrabar then closed
  back inside, and surface a `sweep_direction` ("up"/"down"/none) field. Keep `StructureSense`
  pure — it just reads the new keys.
- **Scope discipline:** this only *enriches evidence and the existing fakeout/at_extreme
  verdicts*. It must **not** silently become a fade signal — that's 12.3.

### 12.3 — Structure/sweep → DirectionSense · RESEARCH TICKET, GATED (do not bolt on)
The review's headline idea: "swept PDH + rejection → fade to PE." `DirectionSense` today is
**VWAP + 5m momentum only** ([direction.py:47-72](../strategy_app/senses/direction.py#L47-L72))
and cannot express this. It is the only part with real P&L potential — **and the most
dangerous**, for three reasons baked into our own findings:
1. Direction is *the* bottleneck and **more inputs have not helped** (depth/OFI didn't; structural
   direction ~0.56, below cost break-even — §1, §10).
2. "Sweep + rejection → fade" is a **mean-reversion** signal living inside a **momentum**
   (compression→expansion) engine. Wrong-regime application is net-negative.
3. Sample is **7–8 quiet days** — on them, breakout did *not* predict a bigger move (n=3),
   which is precisely why `W_STRUCT` is modest, not a gate.

**Therefore:** treat as a **pre-registered, regime-gated direction experiment**, never a merge
into the live direction vote. Pre-register (hypothesis, levels, IS/OOS split, ship-gate) in a
new `docs/` spec à la the R1S hypotheses; measure sweep-fade accuracy *conditioned on the
loaded gate* in [direction_research.py](../ops/research/direction_research.py) before any code
touches `DirectionSense`. If it can't beat ~0.55 on held-out days, it dies in research.

**Status:** 12.1 + 12.2 are safe to implement when build resumes (still HALTED, still sim-gated
per §8). 12.3 is queued as research only. None of this changes the Phase-0 gate or the "big-move
first, direction after" doctrine.

---

## Appendix — data & reproduction (so the numbers are traceable)

- **Data:** `trading_ai.phase1_market_snapshots` (mongo on the runtime VM) — full 25-strike chain + futures bar + chain/ladder aggregates, 1-min, persisted for *all* bars. **7 live days: 2026-05-26, 05-27, 06-01..06-05** (~2,400 bars). Quiet/low-vol regime — treat lifts as directional until more days accrue.
- **Proof script:** [`ops/research/bigmove_score_backtest.py`](../ops/research/bigmove_score_backtest.py) — pure price/volume/OI, no ML/engine. Run inside the strategy_app container (`docker cp` → `docker exec python`); reads mongo directly.
- **Headline numbers (10-min horizon, target 100 pt):** base 34% · `loaded` 49% (n=229, mean 117 pt, 11% ≥200 pt). 5-min horizon ≈ base (dead). depth/OFI (`market_depth_ticks.qty_imbalance`) did not help *timing*.
- **Repro caveat:** the per-bar feature pipeline for the *ML* models needs the engine (`SnapshotAccessor` + `project_stage_views_v2`); the BigMoveScore needs none — it's the simpler, more robust path on purpose.

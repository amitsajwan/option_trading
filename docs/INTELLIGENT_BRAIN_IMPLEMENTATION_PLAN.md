# Intelligent Brain — Architect Review & Implementation Plan

**Date:** 2026-06-06 · **Role:** Architect (owns architecture) · **Companion:** [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md) · **Board:** [INTELLIGENT_BRAIN_SCRUM_BOARD.md](INTELLIGENT_BRAIN_SCRUM_BOARD.md)
**System state:** HALTED (correct). Phase 0 is the hard gate; nothing touches live until it and the cost-aware e2e backtest clear.

This document does three things: (1) **verifies** every concrete claim in the handover against the actual code, (2) records the **architectural decisions** the handover left open (and the conflicts it didn't notice), and (3) lays out a **phase-by-phase implementation plan** mapped to real modules.

---

## 1. Verification — handover claims vs the codebase

I read every referenced file. Status legend: ✅ verified · ⚠️ verified-with-caveat · ❌ wrong/missing · ➕ undocumented finding.

| # | Handover claim | Reality | Verdict |
|---|---|---|---|
| 1 | `TraderDayType` (TREND/REVERSAL/BALANCED/NO_TRADE) at `strategy_app/market/trader_judgement.py:13` | Exact enum present at line 13; `TraderAnnotationRecord` with `invalidation_reference` at line 42 | ✅ |
| 2 | Direction contract is `direction:str=""` only, needs UNKNOWN — `contracts_app/decision_events.py:99` | `DirectionDecisionEvent.direction: str = ""` at line 99, plus `confidence`, `vetoed`, `reason` | ✅ |
| 3 | BigMoveScore proof script `ops/research/bigmove_score_backtest.py` (pure price/vol/OI, no ML) | Present, matches description exactly: `loaded = compression AND oi_build`, `released = velocity AND volume`, 10-min horizon, reads `phase1_market_snapshots` | ✅ |
| 4 | `released` trigger never fired (too strict) | Confirmed in code: `released = velocity and volume` on the **same bar** (line 80), with a `# TODO: too strict` note | ✅ |
| 5 | Compression / Expansion "✅ proven" as senses | The *math* is proven in the backtest script, but there is **no reusable sense module** — the logic lives inline in a research script only | ⚠️ |
| 6 | IntradayRegime "to build" | `strategy_app/market/regime.py` already has `Regime` with `CHOP/BREAKOUT/PANIC/DEAD_MARKET/HIGH_VOL` + `RegimeClassifier` returning `{regime, confidence, reason, evidence}` | ⚠️ extend, don't build |
| 7 | Destination sense "to build (new — key gap)" | No support/resistance/room module exists. Genuinely missing. | ✅ (real gap) |
| 8 | Cost = 1.3% round-trip + theta; training used 6 bps | `strategy_app/cost_model.py` exists: brokerage ₹20/order + 2.5 bps charges + 7.5 bps slippage per side. This is the realistic model — **but the Phase-0 proof script applies NO cost at all** (it only measures point-moves) | ⚠️ cost model exists; not yet in the move proof |
| 9 | Exit floor + scalper "fixed in-container" | `strategy_app/position/exit_policy.py` has a composable stack (PremiumTarget→Trailing→ThesisFail); `EXIT_MAX_LOSS_PCT` wired. "In-container" = not all committed as default | ⚠️ verify what's committed vs hot-patched |
| 10 | Risk tracker exists | `strategy_app/position/tracker.py` + `risk/config.py` + `consumers/risk_decision_consumer.py` | ✅ |
| 11 | 6-stage decision pipeline with structured events | Confirmed: consumers for regime→entry→direction→depth→strike→execution + risk, each emitting a `*DecisionEvent` with the 8-field `BaseDecisionEvent` envelope | ✅ (strong foundation) |
| 12 | Sim harness (e2e on unfiltered mongo) | `ops/sim/run_sim_publisher.py` + `strategy_app/sim/multi_day_runner.py` + exit_replay | ✅ |

### ➕ The undocumented finding the handover missed (most important)

**There is already a `TradingBrain` (`strategy_app/brain/brain.py`), and it conflicts with the new design.**

- It exists today with `consensus`, `session_memory`, `context`, `fitness`, `plugin`, and an `LLMContextProvider` stub — i.e. a real Layer-2/Layer-3 skeleton already shipped.
- **But its core output is a `size_multiplier` (1.0 / 0.85 / 0.5 / 0.0).** The handover's §6 is explicit and non-negotiable: *"Size is always 1 lot — there is no sizing decision. The only risk lever is selectivity."*
- Its `gate_entry()` policy is `day_score + consensus`, **not** the `regime→move→conflict→direction→destination→opportunity` policy of §6.

This is not a small detail. We must decide whether the new decision brain **replaces, wraps, or repurposes** the existing one — and either way, **the sizing lever must be retired** to match the new doctrine. See Decision D1.

---

## 2. Architectural decisions (what I'm locking, as the owner)

These resolve the ambiguities and conflicts above. They are binding unless the data forces a change.

**D1 — Repurpose the existing brain; kill sizing.**
Keep the proven scaffolding (`consensus.py`, `session_memory.py`, `context.py`, `plugin.py`, trace logging). Introduce a new **`DecisionBrain`** that implements the §6 policy and emits a fixed `size = 1 lot`. `size_multiplier` is frozen at `1.0` and the selectivity gate (`OpportunityQuality`) becomes the only risk lever. The old `TradingBrain` is demoted to a Layer-3 *session-context provider* (morning posture / carry / day-avoid), not a sizing authority.

**D2 — Senses are pure functions in a new `strategy_app/senses/` package.**
Each sense returns one dataclass: `SenseVerdict{verdict, confidence, evidence: dict, value}`. No sense imports another sense (independence is the whole point). Senses **wrap** existing logic rather than fork it: `IntradayRegime` wraps `RegimeClassifier`; `DayPersonality` wraps `TraderDayType`; `Cost/EV` wraps `cost_model.py`. Compression/Expansion/Move are extracted from the research script into tested functions.

**D3 — Phase 0 is a true gate and it is not yet passed.**
The `loaded` pair calibrates (49% vs 34% base, n=229). **The gate is `loaded` holding ≥1.4× base on the accrued sample — not a working `released` trigger.** The `released` trigger is currently broken (never fired); re-specifying it (velocity OR volume, or a 2–3 bar window) is an *optional refinement to test*, and **"release adds nothing → use `loaded` alone" is a fully acceptable outcome.** Do **not** force-fit a release trigger to pass the gate. If a larger sample shows `loaded` no longer beats base → STOP, the whole program pauses.

**D4 — Cost realism is enforced at the proof boundary, not assumed.**
The Phase-0 move proof stays cost-free *on purpose* (it measures opportunity, not P&L). But the **Phase-2 e2e backtest MUST route every simulated trade through `cost_model.py`** (no 6 bps anywhere). The go/no-go number is *net* P&L after brokerage + charges + slippage + theta.

**D5 — Direction is built last and abstains by default.**
Extend `DirectionDecisionEvent` to a first-class `side ∈ {CE, PE, UNKNOWN}` + `confidence`. A low-confidence CE **is** UNKNOWN → WAIT, never a blind trade.

**The Phase-2 go/no-go (B-2.6) must be a *conditional/sensitivity* test, not a naive-direction break-even.** With ~50/50 placeholder direction and the asymmetric option payoff (wrong side ≈ −7%, right side ≈ +4%), the entry+destination path is *structurally negative no matter how good the move detector is* — because direction is exactly the component this plan defers to Sprint 4. A hard "net ≥ break-even with naive direction" gate would therefore STOP the program for the wrong reason. **Correct gate: report net P&L as a function of assumed direction accuracy** (e.g. "net = +X at 58% direction, −Y at 50%") and pass if **either** the path is break-even-or-better under a realistic structural-bias direction **or** it cleanly quantifies that *direction is the only remaining gap*. STOP only if the move+destination+cost path is unprofitable *even with direction held perfect*.

**D6 — No LLM in the per-bar path, ever.** Layers 1–2 are deterministic (<1s). Layer 3 (oversight) stays a deferred, human-first convenience. The `LLMContextProvider` stub stays a stub until the deterministic system is profitable.

**D7 — Every bar writes a reasoning trace** (trade and no-trade), reusing the existing `*DecisionEvent` envelope + trace_id plumbing. This is the dataset Layer 3 will eventually learn from; it costs us nothing to start now.

---

## 3. Target architecture (mapped to real modules)

```
Layer 3  OVERSIGHT (human now; LLM deferred)   strategy_app/brain/  (repurposed: session posture, narrative)
            │ reads traces, proposes threshold changes (sim-gated, never auto-live)
Layer 2  DECISION BRAIN (deterministic, per bar)  strategy_app/brain/decision_brain.py  (NEW)
            │  ConflictAnalysis + OpportunityQuality + Destination gate → TRADE/WAIT/SKIP, side, size=1
            │  reuses: consensus.py, trace envelope (contracts_app/decision_events.py)
Layer 1  SENSES (pure fns, parallel)            strategy_app/senses/  (NEW package)
            DayPersonality → wraps trader_judgement.TraderDayType
            IntradayRegime → wraps market/regime.RegimeClassifier
            Compression / Expansion / Move → extracted from ops/research/bigmove_score_backtest.py
            Destination → NEW (support/resistance/room)
            Direction → extends ml/entry_direction_resolver.py + UNKNOWN contract
            Flow/OFI → market/depth_context.py
            Cost/EV → wraps cost_model.py
            Risk → wraps position/tracker.py
            Execution → market/depth_context.py
```

The decision-event pipeline (`strategy_app/consumers/*`) is the wiring that already exists — senses plug into it as verdict producers; the decision brain consumes the verdicts.

---

## 4. Phase plan (mapped, gated, with exit criteria)

> **Gate at every phase:** sim-validate on full raw mongo data before anything touches live.

### Phase 0 — Move-score calibration proof **(HARD GATE — in progress, not passed)**
- Emit the full dose-response table: median/p75/p90 + hit-rate for 50/100/200 pt **per score bucket**, requiring monotonicity (or an explanation per non-monotonic bucket).
- *Optionally* test a re-specified `released` trigger (velocity **OR** volume, and/or a 2–3 bar window) — but only as a refinement; **"release adds nothing, use `loaded` alone" is an acceptable pass.** Do not force-fit a trigger to pass.
- **Exit criteria (the gate):** `loaded` still beats base by **≥1.4× on ≥100 pt** on the accrued sample. If not → STOP. (A working `released` trigger is *not* required to pass.)

### Phase 1 — Senses as pure functions
- Create `strategy_app/senses/` with `SenseVerdict` + one module per sense (§3).
- Extract Compression/Expansion/Move from the research script into tested functions (single source of truth — the script then imports them).
- Wrap (don't fork) regime, day-personality, cost, risk.
- **Build Destination** (the real new sense): nearest support/resistance, `available_space_up/down`, `space_to_move_ratio`. **Levels source must be a runtime feed that's always present in the snapshot/sim — not just `invalidation_reference` (which may be empty in sim).** Primary: the **OI walls already in `chain_aggregates`** (`max_pain`, `ce_oi_top_strike`, `pe_oi_top_strike` — confirmed present in live data), plus prior-day high/low and the opening range (`opening_range` in the snapshot). Use `invalidation_reference` only as an optional overlay when the annotation path runs.
- **Exit criteria:** every sense unit-tested, returns structured evidence, abstains when unsure; Move function reproduces the Phase-0 numbers exactly.

### Phase 2 — Decision brain + traces + cost-aware e2e backtest **(GO/NO-GO GATE)**
- New `DecisionBrain` implements the §6 policy: `regime.alive → move.score/released → ConflictAnalysis → direction UNKNOWN → Destination space → OpportunityQuality → TRADE size=1`.
- Implement **ConflictAnalysis** and **OpportunityQuality** as Layer-2 logic (they peek at all senses — not senses themselves).
- Every bar writes a reasoning trace (reuse decision-event envelope).
- **Backtest end-to-end on the live days through `cost_model.py`**: net P&L + 10-min exit + full cost, reported as a **sensitivity curve over assumed direction accuracy** (50% / 55% / 58% / 60% / perfect).
- **Exit criteria (conditional — see D5):** PASS if net P&L (after cost) is break-even-or-better under a realistic structural-bias direction, **or** the curve cleanly shows direction is the *only* remaining gap (path is profitable at an achievable accuracy). STOP only if it's negative *even with direction held perfect* (then the move/destination/cost path itself is the problem).

### Phase 3 — Direction sense (UNKNOWN-first-class)
- Extend `DirectionDecisionEvent` (D5): `side ∈ {CE,PE,UNKNOWN}`, `confidence 0..1`, `basis[]`.
- Build structural direction (VWAP / OFI / CE-PE depth) in `senses/direction.py`; A/B vs the existing ML resolver; abstain → WAIT.
- **Exit criteria:** direction sense beats ~0.55 on accrued live data, OR we adopt the documented structural-CE-bias + abstain fallback and trade far fewer, cleaner setups.

### Phase 4 — Exit as a sense
- Make exits regime/horizon-aware in `position/exit_policy.py` (10-min hold for a loaded breakout; tight for a fade). Prove the giveback fix holds in the **e2e** backtest, don't assume it.

### Phase 5 — Oversight (DEFERRED)
- Human reads traces; simple rule adjustments only. No LLM until the deterministic system is profitable and hand-reading is the bottleneck.

### Phase 6 — Shadow → live
- Paper/shadow for weeks; size up only on proven live edge. Size stays 1 lot (D1).

---

## 5. Top risks I'm tracking as owner

1. **Sample size (7 quiet days, ~2,400 bars).** All lifts are directional until OOS / more days accrue. Mitigation: data keeps accruing daily; re-run Phase 0 weekly; no real size until OOS confirms.
2. **Sizing-lever conflict (D1).** If the old `TradingBrain` sizing path stays live anywhere, it silently violates doctrine. Mitigation: freeze `size_multiplier=1.0` explicitly and assert it in a test.
3. **Capture ≠ opportunity.** 59% see a 100-pt move; the move can revert. Phase 4 must prove holding winners in the e2e backtest.
4. **Direction may stay hard (~0.55 ceiling).** Honest fallback (D5) is structural bias + abstain.
5. **Latency creep.** Any LLM in the per-bar path makes the system untradeable. Enforced by D6 + a latency test in Phase 2.
6. **Cost honesty.** Easy to "prove" edge by under-costing. D4 enforces `cost_model.py` at the e2e boundary.

---

## 6. Team allocation rationale (for the scrum board)

Three agent teams, assigned to play to strengths and minimise cross-team merge conflicts:

- **CODEX** — deterministic, self-contained code: the Phase-0 proof fix, pure-function senses, the Destination math, backtest scripts. Clear inputs/outputs, heavy unit tests.
- **CURSOR** — repo-wide integration: wiring senses into the consumer pipeline, contract changes (`DirectionDecisionEvent`), repurposing the brain, the e2e sim run, killing the sizing lever. Needs broad codebase context.
- **CLAUDE** — reasoning/research/calibration + the (deferred) oversight narrative: monotonicity analysis, ConflictAnalysis + OpportunityQuality design, trace schema, risk audits, docs.

See [INTELLIGENT_BRAIN_SCRUM_BOARD.md](INTELLIGENT_BRAIN_SCRUM_BOARD.md).

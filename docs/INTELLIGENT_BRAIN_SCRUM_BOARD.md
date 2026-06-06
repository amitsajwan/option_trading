# Scrum Board — Intelligent Brain

**Living document** — update Status / Owner / Results after each merge or sim run.
**Last updated:** 2026-06-06 (board created) · **Role:** Project Manager
**Companions:** [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md) (vision) · [INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md](INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md) (architecture + decisions)

> **System is HALTED.** Phase 0 (B-0) and the cost-aware e2e backtest (B-2.6) are **hard gates**. Nothing downstream of a gate starts until the gate is green. Nothing touches live.

---

## How to use this board

1. Pick a story from your team column → set **Owner** → move to **In progress**.
2. Check off **Tasks**; link run IDs / commit SHAs in **Results**.
3. When **Acceptance criteria** are met, move to **Done** and paste metrics.
4. **Respect gates and dependencies** — a story cannot start until its `depends-on` is Done.

**Status:** `Backlog` · `Ready` · `In progress` · `In review` · `Done` · `Blocked` · `Cancelled`
**Priority:** `P0` (blocking gate) · `P1` (this sprint) · `P2` (next) · `P3` (later)

---

## Teams

| Team | Charter | Why |
|---|---|---|
| **CODEX** | Deterministic, self-contained code: proof fixes, pure-function senses, Destination math, backtest scripts | Clear I/O, heavy unit tests, low cross-file blast radius |
| **CURSOR** | Repo-wide integration: pipeline wiring, contract changes, brain repurpose, e2e sim, retire sizing lever | Needs broad codebase context + safe refactors |
| **CLAUDE** | Reasoning/research/calibration + deferred oversight: monotonicity analysis, ConflictAnalysis + OpportunityQuality design, trace schema, risk audits, docs | Reasoning-heavy, design-first work |

---

## Sprint plan & gate map

```
Sprint 1 (GATE)   B-0  Move-score calibration proof  ───────────────┐  HARD GATE
                                                                     ▼
Sprint 2          B-1.x  Senses as pure functions (parallel)
Sprint 3 (GATE)   B-2.x  Decision brain + traces + cost-aware e2e ───┐  GO/NO-GO
                                                                     ▼
Sprint 4          B-3.x  Direction sense (UNKNOWN-first)
Sprint 5          B-4.x  Exit as a sense
Deferred          B-5.x  Oversight   ·   B-6.x  Shadow→live
```

---

# SPRINT 1 — Phase 0 calibration proof  ★ HARD GATE ★

### B-0.1 · Fix the `released` trigger & emit dose-response — `[CODEX]` · **P0**
**Status:** Ready · **Owner:** _@codex_ · **File:** `ops/research/bigmove_score_backtest.py`
**Why:** `released = velocity AND volume` (same bar) never fires (line 80). The `loaded` pair already calibrates (49% vs 34% base); we need the timing trigger fixed before any sense is built.
**Tasks:**
- [ ] Per score-bucket table: median, p75, p90, hit-rate for 50/100/200 pt.
- [ ] Add monotonicity check (bigger score → bigger move) with per-bucket explanation.
- [ ] Re-run on all accrued live days (currently 7; add any new).
- [ ] *(Optional refinement)* test a re-spec'd `released` (velocity **OR** volume, and/or 2–3 bar window). "Release adds nothing → use `loaded` alone" is an **acceptable** result — do **not** force-fit a trigger to pass.
**Acceptance (the gate):** dose-response table printed AND **`loaded` still ≥1.4× base on ≥100 pt** on accrued data. A working `released` trigger is **not** required to pass.
**Results:** _link run output_

### B-0.2 · Calibration analysis & gate decision memo — `[CLAUDE]` · **P0** · depends-on: B-0.1
**Status:** Backlog · **Owner:** _@claude_
**Tasks:**
- [ ] Interpret B-0.1 output; confirm/deny monotonicity claim.
- [ ] Write the GO / STOP recommendation (if `loaded` no longer beats base → STOP whole program).
- [ ] Append findings to the implementation plan §4 Phase 0.
**Acceptance:** a one-page GO/STOP memo with the numbers; architect sign-off recorded here.

### B-0.3 · Data freshness check — `[CURSOR]` · **P1**
**Status:** Backlog · **Owner:** _@cursor_
**Tasks:**
- [ ] Confirm `trading_ai.phase1_market_snapshots` count per day; report new days since 06-05.
- [ ] Document repro (docker cp → docker exec) so any team can re-run B-0.1.
**Acceptance:** day/bar counts posted; repro steps verified.

> **GATE:** B-0.2 must be GO before any Sprint-2 story moves to In progress.

---

# SPRINT 2 — Senses as pure functions

### B-1.0 · `senses/` package + `SenseVerdict` contract — `[CURSOR]` · **P0**
**Status:** Backlog · **Owner:** _@cursor_ · depends-on: B-0.2(GO)
**Tasks:**
- [ ] Create `strategy_app/senses/__init__.py` + `SenseVerdict{verdict, confidence, evidence, value}` dataclass.
- [ ] Establish the "one job, no peeking, always-abstain-allowed" rules in a module docstring + a base test.
**Acceptance:** contract merged; example sense + test green. **This unblocks all B-1.x.**

### B-1.1 · Compression / Expansion / Move senses (extract from proof) — `[CODEX]` · **P0** · depends-on: B-1.0
**Status:** Backlog · **Owner:** _@codex_
**Tasks:**
- [ ] Extract compression, expansion, and the Move function into `senses/move.py` as tested pure functions.
- [ ] Have `ops/research/bigmove_score_backtest.py` import them (single source of truth).
- [ ] `MoveVerdict` returns score/components/expected_move_pt/prob_100/prob_200/horizon/confidence (handover §5).
**Acceptance:** senses reproduce B-0.1 numbers exactly; unit tests cover boundary bars.

### B-1.2 · **Destination** sense (NEW — key gap) — `[CODEX]` · **P0** · depends-on: B-1.0
**Status:** Backlog · **Owner:** _@codex_
**Tasks:**
- [ ] `senses/destination.py`: nearest support/resistance, `available_space_up/down`, `expected_move_pt`, `space_to_move_ratio`.
- [ ] **Primary levels = always-present runtime feeds:** OI walls in `chain_aggregates` (`max_pain`, `ce_oi_top_strike`, `pe_oi_top_strike`) + prior-day high/low + `opening_range`. Use `invalidation_reference` (`trader_judgement.py:42`) only as an optional overlay (may be empty in sim).
- [ ] Unit tests incl. "loaded but no space" case.
**Acceptance:** returns structured verdict with evidence; **levels resolve on raw sim snapshots without the annotation path**; backtested on live days; no peeking at other senses.

### B-1.3 · IntradayRegime + DayPersonality (wrap existing) — `[CURSOR]` · **P1** · depends-on: B-1.0
**Status:** Backlog · **Owner:** _@cursor_
**Tasks:**
- [ ] `senses/regime.py` wraps `market/regime.RegimeClassifier` → `{state: alive/compressed/expanding/dead/chaotic, reason}`.
- [ ] `senses/day_personality.py` wraps `TraderDayType` → `{type, confidence}`.
**Acceptance:** thin adapters, no logic fork; tests assert mapping from existing labels.

### B-1.4 · Cost/EV + Risk senses (wrap existing) — `[CODEX]` · **P1** · depends-on: B-1.0
**Status:** Backlog · **Owner:** _@codex_
**Tasks:**
- [ ] `senses/cost_ev.py` wraps `cost_model.py` → `{exp_move_pt, net_after_cost, +ev}`.
- [ ] `senses/risk.py` wraps `position/tracker.py` → `{ok, daily_dd, consec_losses, in_position}`.
**Acceptance:** no 6 bps anywhere; cost numbers match `cost_model.py`.

### B-1.5 · Sense-suite design review — `[CLAUDE]` · **P1** · depends-on: B-1.1..B-1.4
**Status:** Backlog · **Owner:** _@claude_
**Tasks:**
- [ ] Audit independence (no sense imports another).
- [ ] Confirm every sense can abstain; evidence is sufficient to explain a decision in one sentence.
**Acceptance:** review notes posted; any violations filed as fix stories.

---

# SPRINT 3 — Decision brain + traces + e2e backtest  ★ GO/NO-GO GATE ★

### B-2.1 · ConflictAnalysis + OpportunityQuality design — `[CLAUDE]` · **P0** · depends-on: B-1.5
**Status:** Backlog · **Owner:** _@claude_
**Tasks:**
- [ ] Spec ConflictAnalysis cases (handover §6a): `move_strong_but_direction_conflicted`, `ofi_bullish_price_falling`, `velocity_up_volume_weak`, `loaded_but_no_space`.
- [ ] Spec OpportunityQuality edge formula + 0..10 ranking (§6b).
**Acceptance:** written spec with worked examples → handed to CURSOR for B-2.2.

### B-2.2 · `DecisionBrain` implements §6 policy — `[CURSOR]` · **P0** · depends-on: B-2.1
**Status:** Backlog · **Owner:** _@cursor_ · **File:** `strategy_app/brain/decision_brain.py` (NEW)
**Tasks:**
- [ ] Implement the policy ladder: regime.alive → move.score/released → conflict → direction UNKNOWN → destination space → opportunity edge → TRADE size=1.
- [ ] Reuse `brain/consensus.py` where it fits; do **not** reuse `size_multiplier`.
- [ ] Output `TRADE/WAIT/SKIP`, side, fixed `size=1`.
**Acceptance:** unit tests cover every policy branch incl. WAIT-on-UNKNOWN and SKIP-on-no-room.

### B-2.3 · Retire the sizing lever (Decision D1) — `[CURSOR]` · **P0** · depends-on: B-2.2
**Status:** Backlog · **Owner:** _@cursor_ · **File:** `strategy_app/brain/brain.py`
**Why:** existing `TradingBrain` emits `size_multiplier` (0.5/0.85/1.0) — violates "always 1 lot, selectivity only."
**Tasks:**
- [ ] Freeze `size_multiplier=1.0`; demote old brain to a session-context provider.
- [ ] Add a regression test asserting size is always 1 lot.
**Acceptance:** no live path can size ≠ 1 lot; test green.

### B-2.4 · Reasoning-trace writer (every bar) — `[CURSOR]` · **P1** · depends-on: B-2.2
**Status:** Backlog · **Owner:** _@cursor_
**Tasks:**
- [ ] Write a trace per bar (trade AND no-trade) reusing the `*DecisionEvent` envelope (`contracts_app/decision_events.py`).
- [ ] Include all sense verdicts + the policy branch taken.
**Acceptance:** traces persist for a full sim day; replayable.

### B-2.5 · Trace schema for future oversight — `[CLAUDE]` · **P2** · depends-on: B-2.4
**Status:** Backlog · **Owner:** _@claude_
**Tasks:**
- [ ] Define the trace fields the (deferred) oversight layer will learn from; document it.
**Acceptance:** schema doc merged; B-2.4 conforms.

### B-2.6 · Cost-aware end-to-end backtest — `[CURSOR]` · **P0** · depends-on: B-2.2, B-2.4 · ★ GO/NO-GO ★
**Status:** Backlog · **Owner:** _@cursor_ · **Files:** `ops/sim/run_sim_publisher.py`, `strategy_app/sim/multi_day_runner.py`
**Tasks:**
- [ ] Run the brain over live days through `cost_model.py` (brokerage + charges + slippage + theta).
- [ ] 10-min exit; report net P&L as a **sensitivity curve over assumed direction accuracy** (50/55/58/60/perfect) — real direction comes in Sprint 4.
- [ ] Add a per-bar latency assertion (<1s, no LLM in path — Decision D6).
**Acceptance (conditional — Decision D5):** PASS if net P&L (after cost) is break-even-or-better under a realistic structural-bias direction, **OR** the curve shows direction is the *only* gap (profitable at an achievable accuracy). **STOP only if negative even with direction held perfect.** Do **not** STOP merely because naive 50/50 direction loses — that's the Sprint-4 component, by design.
**Results:** _paste the net-P&L-vs-direction-accuracy curve, trade count, latency_

> **GATE:** B-2.6 must be GO before Sprint 4 starts.

---

# SPRINT 4 — Direction sense (UNKNOWN-first-class)

### B-3.1 · Extend `DirectionDecisionEvent` contract — `[CURSOR]` · **P0** · depends-on: B-2.6(GO)
**Status:** Backlog · **Owner:** _@cursor_ · **File:** `contracts_app/decision_events.py:99`
**Tasks:**
- [ ] Add first-class `side ∈ {CE,PE,UNKNOWN}`, `confidence 0..1`, `basis[]`; keep back-compat.
- [ ] Low-confidence CE maps to UNKNOWN at the contract/consumer boundary.
**Acceptance:** contract + consumer tests green; UNKNOWN propagates to WAIT.

### B-3.2 · Structural direction sense — `[CODEX]` · **P0** · depends-on: B-3.1
**Status:** Backlog · **Owner:** _@codex_ · **Files:** `senses/direction.py`, extends `ml/entry_direction_resolver.py`, `market/depth_context.py`
**Tasks:**
- [ ] Build VWAP / OFI / CE-PE depth structural direction; abstain when unclear.
- [ ] A/B vs existing ML resolver on accrued live data.
**Acceptance:** beats ~0.55, OR documents the structural-CE-bias + abstain fallback.

### B-3.3 · Direction A/B analysis — `[CLAUDE]` · **P1** · depends-on: B-3.2
**Status:** Backlog · **Owner:** _@claude_
**Tasks:**
- [ ] Compare structural vs ML; recommend ship/fallback; quantify WAIT rate.
**Acceptance:** memo with the decision; board updated.

---

# SPRINT 5 — Exit as a sense

### B-4.1 · Regime/horizon-aware exits — `[CODEX]` · **P1** · depends-on: B-2.6(GO)
**Status:** Backlog · **Owner:** _@codex_ · **File:** `strategy_app/position/exit_policy.py`
**Tasks:**
- [ ] 10-min hold for a loaded breakout; tight exit for a fade.
- [ ] Prove the giveback fix in the e2e backtest (don't assume).
**Acceptance:** e2e net P&L improves or holds vs B-2.6 with winners held longer.

### B-4.2 · Confirm committed vs hot-patched exit floor — `[CURSOR]` · **P2**
**Status:** Backlog · **Owner:** _@cursor_
**Tasks:**
- [ ] Verify `EXIT_MAX_LOSS_PCT` floor + scalper are committed defaults, not container-only patches.
**Acceptance:** defaults in repo; documented.

---

# DEFERRED — Oversight & live

### B-5.1 · Oversight (human-first; LLM deferred) — `[CLAUDE]` · **P3**
Read traces by hand; propose sim-gated threshold tweaks only. No LLM in path. Build only when hand-reading traces becomes the bottleneck (handover §7, Decision D6).

### B-6.1 · Shadow → live — `[CURSOR]` · **P3** · depends-on: B-3.3, B-4.1
Paper/shadow for weeks; size stays 1 lot; size up only on proven live edge.

---

## Done

_(none yet — board created 2026-06-06)_

## Blocked

_(none)_

## Risk register (PM view — mirrors plan §5)

| Risk | Owner | Mitigation |
|---|---|---|
| 7 quiet days only | CLAUDE | weekly Phase-0 re-run as data accrues; no size until OOS |
| sizing-lever conflict | CURSOR | B-2.3 freezes size=1 + test |
| capture ≠ opportunity | CODEX | B-4.1 must prove holding winners e2e |
| direction ~0.55 ceiling | CODEX/CLAUDE | B-3.2 abstain fallback |
| latency creep | CURSOR | B-2.6 latency assertion |
| under-costing | CURSOR | B-2.6 routes through `cost_model.py` |

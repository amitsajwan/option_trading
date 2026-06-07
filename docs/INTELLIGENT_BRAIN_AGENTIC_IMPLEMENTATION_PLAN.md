# Intelligent Brain — Agentic Implementation Plan

**Date:** 2026-06-07 · **Status:** plan only — deterministic core stays HALTED; LLM enters at Phase 3, behind flags · **Companions:** [INTELLIGENT_BRAIN_AGENTIC_ARCHITECTURE.md](INTELLIGENT_BRAIN_AGENTIC_ARCHITECTURE.md), [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md), [INTELLIGENT_BRAIN_LLM_OVERSIGHT.md](INTELLIGENT_BRAIN_LLM_OVERSIGHT.md)

Synthesised from three review lenses (trader / architect / prompt-expert). The order is deliberate: **deterministic value first, LLM last**, each phase shippable behind a flag and independently useful.

---

## Cross-cutting principles (apply to every phase)

| # | Principle | Owner lens |
|---|---|---|
| P1 | **Not every reflection needs the LLM.** A deterministic check runs first; the LLM is invoked only when the check is ambiguous. | trader + prompt |
| P2 | **Facts-not-memory.** Every prompt is fed verified numbers; "mark unknown, never invent." | prompt |
| P3 | **Reasoning is event-triggered and async**, enqueued *after* the fast lane acts. Bounded queue, drop-oldest, degrade-to-no-op. | architect |
| P4 | **Inert by default.** Agent output = annotations + proposals; no path to live config without sim + human. | architect + trader |
| P5 | **Everything is evaluable.** Log `{inputs → output → realized outcome}`; back-test tags/proposals. Versioned prompts pinned in every trace. | prompt |
| P6 | **Senses stay pure** (no `now()`, no mutation) so fast lane and agent share them safely. | architect |
| P7 | **No new size lever.** `size_multiplier` frozen at 1.0; selectivity is the only risk knob. | architect + handover D1 |

**Gating discipline:** Phases 0–2 are deterministic infra + journaling — safe to build now, useful regardless of the LLM. **Phases 3+ should not start until the deterministic edge is profitable / unhalted** (handover: the agent is a convenience, never a dependency).

---

## Phase 0 — Foundations (no LLM)

**Goal:** a durable memory substrate + doctrine guardrails.

- **0.1** Freeze `size_multiplier=1.0` and add an asserting test (closes the D1 sizing-path violation).
- **0.2** `TraceStore`: append-only, `schema_version`-tagged, replay-safe. Reuse the storage contract (JSONL canonical, mongo derived). Schema: `trace_id, ts, kind∈{bar,entry,exit,...}, senses{}, decision, outcome|null`.
- **0.3** Wire the engine to write `entry` and `exit` traces (extend `contracts_app/decision_events.py`); `outcome` (pnl, mfe, mae, time-in-trade, cost) filled on close.

**Exit criteria:** a full sim day's entries/exits persist, are queryable, survive replay (drift=0). Tests green. **No LLM, no live-path change.**

## Phase 1 — Sense registry + read-only tools (no LLM)

**Goal:** one sense implementation, two callers.

- **1.1** Extract senses into pure functions in `strategy_app/senses/` returning `SenseVerdict`; **purity tests** (no clock, no mutation, deterministic for fixed input).
- **1.2** `ToolRegistry` + wrap senses as `permission="read"` tools.
- **1.3** Fast-lane decision brain calls the registry (no behavior change — refactor + characterization tests).

**Exit criteria:** fast lane uses the registry; read tools return identical verdicts; Phase-0 numbers reproduce exactly.

## Phase 2 — Deterministic reflection layer (still no LLM)

**Goal:** mechanical post-trade analysis — and the exact feature vector the LLM will later reason over (P1).

- **2.1** `post_exit` deterministic autopsy precursor: compute MFE/MAE, giveback, cost realisation, and **which `SenseVerdict` disagreed with the outcome**. Mechanically tag where unambiguous (e.g. "exit_miss" when MFE ≥ target then gave back).
- **2.2** `post_entry` execution-quality check: realised slippage + charges vs the edge that justified the trade (directly targets the "perceived +0.6% = real −₹46" problem).
- **2.3** Each closed trade's trace gets a deterministic tag + evidence; ambiguous cases flagged `needs_reasoning`.

**Exit criteria:** every closed trade carries a deterministic tag + evidence; the `needs_reasoning` set is the only thing the LLM will ever see. Pure functions, fully tested.

> **Status (2026-06-07): 2.1 + 2.2 BUILT + TESTED.** [`strategy_app/brain/reflection.py`](../strategy_app/brain/reflection.py) — `autopsy()` (→ `LossTag` cost/exit/direction/entry/noise + `needs_reasoning`), `execution_quality()`, and the one-call `reflect()` journal record; `ClosedTrade.from_position()` duck-typed adapter. 17 tests in [`test_reflection.py`](../strategy_app/tests/test_reflection.py), 116-test brain/senses/llm suite green (no regressions). Pure, no LLM, no live-path change. **2.3 (wire into the exit path) deferred** — needs a running session/sim to verify; wire-in point: call `reflect(ClosedTrade.from_position(pos, cost_frac=…, target_frac=…, stop_frac=…, entry_verdicts=…), edge_frac=…)` where the engine closes a position (near `evaluate_playbook_exit` in `playbook_brain.py`) and append the record to the trade's trace.

## Phase 3 — LLM autopsy (first reasoning; slow lane; flagged) — **GATE**

**Goal:** the LLM resolves only the ambiguous losers.

- **3.1** Async event dispatcher: bounded queue, drop-oldest, separate thread; `post_exit` enqueues after close (P3).
- **3.2** `AgentRuntime.on_trigger("post_exit")` — invoked **only** for `needs_reasoning` trades. Input = deterministic features + `SenseVerdict`s. Output = enum `{direction_miss|exit_miss|entry_miss|cost_miss|noise}` + calibrated confidence + evidence pointer (P2, prompt-expert).
- **3.3** Model = `genai_module` (Groq, temp≈0, JSON mode + robust extractor); prompt versioned via `PromptStore`, version pinned in the trace.
- **3.4** **Eval harness** (P5): log `{inputs → tag → realized}`; sample hand-labelled; measure agreement + stability.

**Exit criteria (GATE):** on accrued trades the tags are (a) stable run-to-run, (b) agree with hand-labels on a sample, (c) demonstrably useful (e.g. surface a fixable exit-miss cluster). Degrade-to-no-op verified (LLM down → trade still journaled). **If tags are noise or unfalsifiable → STOP, keep the deterministic layer only.**

## Phase 4 — Pre-open posture → concrete behavior

**Goal:** make the (already-built) morning call *do something*, grounded in real facts.

- **4.1** Deterministic `MarketContextProvider`: yfinance numerics + **computed** expiry/holidays (never model-recalled — proven unreliable). Facts captured to trace.
- **4.2** Feed those facts to `LLMContextProvider`; map `day_assessment` → concrete, **sim-validated** gate deltas (e.g. VOLATILE → +OpportunityQuality threshold) — not free text (trader lens).

**Exit criteria:** posture changes pass the sim, are logged, and are reversible. No assessment → no change (safe default).

## Phase 5 — EOD narrative + propose-only (sim-gated)

**Goal:** the oversight agent that audits the day and *proposes*.

- **5.1** `AgentRuntime.on_trigger("eod")` reads the day's traces → narrative + threshold **proposals** written to an inert proposals store.
- **5.2** Proposals **rate-limited** (e.g. ≤ weekly) and **OOS-sim-gated**; a human applies via the existing deploy path (P4, anti-overfit per trader lens).

**Exit criteria:** a proposal round-trips through the sim and is human-applied; **zero** auto-live path exists.

## Phase 6 — Event watchers (deferred, heavy gate)

`regime_change` / `risk_event` triggers → propose posture/stand-down; event-triggered, off per-bar, human/gate-approved. Build only if Phases 3–5 prove their worth.

---

## Sequencing & ownership

```
Phase 0 ─ Phase 1 ─ Phase 2 ──┬── Phase 3 (GATE) ── Phase 5
   (safe to build now)        └── Phase 4
                                          Phase 6 (deferred)
```

- **Now (safe, deterministic):** Phases 0–2 — memory substrate, sense registry, mechanical reflection. Valuable even if the LLM is never turned on.
- **After the edge is profitable:** Phase 3 gate, then 4–5.
- Flags: `BRAIN_AGENT_ENABLED` (master) + per-trigger toggles; `genai_module` as a **sidecar service** behind a stable HTTP contract.

## What success looks like / what kills it

- **Success:** losses are mechanically + (where ambiguous) LLM-tagged; the eval harness shows the tags surface real, fixable patterns; pre-open posture measurably improves selectivity in sim; proposals are sim-gated and rare.
- **Kills it:** LLM in the per-bar path · reasoning on the 1-minute clock · post-trade reasoning that can delay/veto a trade · any un-gated agent→live write · curve-fitting EOD proposals to a handful of quiet days · building the agent before the deterministic core makes money.

---

## One-paragraph summary

Build the **memory + deterministic reflection** first (Phases 0–2): a durable trace store, a pure shared sense registry, and a mechanical post-trade autopsy that tags most losses (direction vs exit vs cost) and an execution-quality check — all with **no LLM**. Only the *ambiguous* losers reach the LLM (Phase 3, gated): a constrained **enum classification** over real numbers, versioned, evaluated against hand-labels — kept only if it proves measurably useful. Then ground the **pre-open posture** in real facts and wire it to concrete, sim-validated gate changes (Phase 4), and finally an **EOD propose-only** agent whose suggestions are rare, OOS-sim-gated, and human-applied (Phase 5). Every reasoning step is event-triggered, async, off the latency-walled fast lane, inert by default, and falsifiable.

# Tonight's Work Plan — Making the System More Perfect

**Date:** 2026-06-03 evening
**Context:** Market closed, full resources available. Goal: high-leverage improvements
across engine → trace → UI, sequenced so each unblocks the next.

---

## The core finding that drives everything

The engine has **22 distinct entry-block reasons** (verified via grep), but the
decision trace only records **5 generic gates** on a live trade
(`regime → confidence → policy → ranking → execution`). Three gates we built TODAY
are **not traced at all**:

- `direction_evidence_mismatch` (the bull/bear agreement gate)
- `zero_mfe_cooldown`
- `trap_gate` / `strike_veto`

**Consequence:** the gates that actually decide trades are invisible to every UI.
The Trade Inspector and any Flow visualizer are both blind to them. **This is the
root blocker — fix tracing first, and every downstream surface gets richer for free.**

---

## EPIC 1 — Engine: trace every gate that fires (FOUNDATION)

> Without this, no UI can show the real decision. Highest leverage. Do first.

**Story 1.1 — Single source of truth for entry gates.**
Today the block logic lives in `evaluate()` (the 22 `entry blocked: …` logs) while the
*trace* is rebuilt separately in `_entry_candidate_gate_rows` (19 gate_ids). They drift.
- Extract a single ordered `ENTRY_GATES` list where each gate is `{id, group, check(ctx)
  -> pass|block|skip, reason_code, metrics(ctx)}`.
- `evaluate()` runs the list AND the trace is generated from the same list → they can
  never disagree. (This is the v2 gate-cascade design, applied to the live v1 path.)
- **AC:** every `entry blocked: X` log has a matching gate row in the trace with the
  same reason_code; a golden replay shows identical decisions to today.

**Story 1.2 — Add the 3 missing gates to the trace.**
`direction_evidence` (bull/bear), `zero_mfe_cooldown`, `strike_veto` get gate rows with
their real metrics (bull_score, bear_score, mfe, premium vs cap).
- **AC:** click today's 09:48 CHOP trade → see `direction_evidence` with bull=0.8/bear=0.0.

**Story 1.3 — Trace metrics completeness.**
Every gate row carries the numbers behind it (not just pass/block): entry_prob,
regime_confidence, consensus margin, ce/pe score, premium, iv_pct. So the UI never has
to fall back to placeholders.
- **AC:** no UI placeholder (0.50) ever shown for a trade that has a trace.

**Effort:** ~3-4h. **Risk:** medium (core engine) → mitigated by golden-master replay
before/after parity check (`golden_master_v1_v2.py` pattern).

---

## EPIC 2 — Trace persistence for ALL outcomes (not just trades)

> The Flow visualizer needs blocked bars too. Today only `entry_taken` traces are
> reliably linked; blocked bars exist in `strategy_decision_traces` but aren't surfaced.

**Story 2.1 — Persist + serve blocked-bar traces.**
Ensure every evaluated bar writes a trace (blocked/hold/entry), and add a dashboard
endpoint `/api/flow/{date}?run=…` returning per-bar: time, regime, evidence,
final_outcome, primary_blocker_gate, ordered_gates.
- **AC:** API returns ~375 rows for a full day (one per snapshot), each with its outcome.

**Story 2.2 — Live stream (optional, if time).**
A WS topic that pushes each bar's decision as it happens, for a live "flow ticker".
- **AC:** new bar appears in the Flow tab within 2s of the engine evaluating it.

**Effort:** ~2h (2.1), +2h (2.2). **Risk:** low (additive).

---

## EPIC 3 — UI: replace Trade Inspector with a proper Decision surface

> Decision: SEPARATE component, not a merge. Inspector stays lean (per-trade audit);
> Flow is the "how the system thinks" explainer. We were wrong to conflate them.

**Story 3.1 — `FlowPanel` component (the visualizer, reborn live).**
Vertical pipeline per bar (snapshot → gates → outcome), driven by EPIC 2 data:
- Bar picker strip (blocked bars dim, traded bars green) — like the HTML prototype.
- Click a bar → full vertical cascade with each gate's reason + metrics inline.
- Regime evidence bull/bear bars with the agreement verdict.
- Works for live (today) and replay (any date).
- **AC:** parity with `docs/trade_flow_visualizer.html` UX, but from real data.

**Story 3.2 — Slim Trade Inspector back down.**
Revert the in-inspector decision-chain experiment to a tight per-trade card: P&L,
entry/exit, the ONE blocker/winner gate, link to "open in Flow". Collapse sections stay.
- **AC:** inspector fits 320px without scrolling for the common case.

**Story 3.3 — Wire Flow as a tab** (Tape | Flow | Map | More) + deep-link from a trade.
- **AC:** clicking a tape trade can "open in Flow" at that bar.

**Effort:** ~4-5h. **Risk:** low-medium (frontend only).

---

## EPIC 4 — Correctness hardening (parallelizable, pick as time allows)

**Story 4.1 — Direction model retrain gate.** AUC=0.557 is near-random. Either retrain
with current features or formally cap its weight + document the ceiling. At minimum: a
startup assertion that warns when a loaded model's holdout AUC < 0.60.

**Story 4.2 — Exit parity audit.** We tuned scalper/lottery params blind. Run a replay
sweep over 06-01/06-02/06-03 comparing exit configs (target/trail/stop) and pick by
realized MFE-capture, not intuition.

**Story 4.3 — Golden-master CI.** Make `golden_master_v1_v2.py` a one-command check that
any engine change must pass (v1≡v2 or documented divergence) before deploy.

**Story 4.4 — positions.jsonl reconciliation.** The orphaned-open-position issue from
intra-session restarts — add a session-start reconciler that closes stale opens.

**Effort:** 1-2h each. **Risk:** low (analysis/tooling).

---

## Recommended sequence for tonight

```
1. EPIC 1.1 + 1.2  →  engine traces all gates incl. the 3 new ones   [foundation]
2. Verify via golden-master replay (parity)                          [safety]
3. EPIC 2.1        →  serve per-bar flow data (blocked + entered)     [unblocks UI]
4. EPIC 3.1 + 3.3  →  FlowPanel tab from real data                    [the thing you want]
5. EPIC 3.2        →  slim the inspector                              [cleanup]
6. EPIC 4.3        →  golden-master CI guard                          [lock it in]
```

Stories 4.1 / 4.2 / 4.4 are independent — slot in if 1–6 finish early.

## Definition of done for tonight

- Click ANY trade (live or replay) → see the **real** gate that decided it, with numbers.
- A **Flow tab** shows the full per-bar pipeline, blocked bars included.
- Every engine change tonight passes the golden-master parity check.
- No placeholder/fake-fail data anywhere in the UI.

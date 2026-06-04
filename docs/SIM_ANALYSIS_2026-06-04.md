# Live-Strategy Verification — Step-wise Analysis (2026-06-04)

**Method:** trace-driven, full-population, component-by-component. Each ops-sim run
emits one decision trace per bar; we verify each component (entry model, direction
model, gates, regime tagger, exits) over **all** bars — not just taken trades — using
`analyze_sim_trace.py` / the per-decision card (`strategy_app/sim/trace_digest.py`).

**Run under analysis:** ops-sim for 2026-06-04 (latest job `13ff68b7-cba`).
Engine: deterministic, `direction_source=ml_entry_timing` (consensus mode = what
`.env.compose` intends for live). Session ≈ 351–360 one-minute bars (09:34→15:29).

> This is a **living document** — append each verification step. Final section holds
> the consolidated "what needs to be done / where".

---

## STEP 0 — Trace completeness (foundational; FIXED)
- **Finding:** first runs produced only **119 traces for a ~360-bar day**.
- **Evidence:** `diag.evaluated=360` but `decision_traces.jsonl` had 119 lines.
- **Reasoning:** the engine built a trace only when a strategy voted. Bars where the
  entry model declined (`entry_prob < 0.65`) hit `if not votes: return None`
  ([deterministic_rule_engine.py:462](../strategy_app/engines/deterministic_rule_engine.py#L462))
  and were never traced → **analysis was survivorship-biased to fired bars only.**
- **Action (DONE):** build + log a trace on no-vote bars too (commit `6b0c922`). Now
  351/360 traced.
- **Residual:** ~9 exit bars still early-return at
  [:445-446](../strategy_app/engines/deterministic_rule_engine.py#L445) before the
  trace block. **TODO:** trace those for 100% coverage.

## STEP 1 — Entry model — DISCRIMINATES (not the problem)
- **Finding:** fires on **119/351 = 33.9%** of bars; declines on ~66%.
- **Evidence:** digest `ml_entry`: bars_fired 119 / bars_total 351; entry_prob when
  fired min 0.669 / median 0.849.
- **Reasoning:** an earlier "fires 100% / near-constant YES" claim was the Step-0
  bias (only fired bars were counted). With full population the model clearly selects
  ~1/3 of bars → it is working. (Model: `entry_only_model.joblib`, holdout AUC 0.83.)
- **Caveat:** the **232 declined bars' probs (<0.65) are not captured** (the model
  returns None below threshold, [ml_entry.py:179](../strategy_app/engines/strategies/ml_entry.py#L179)),
  so the full distribution + true separation aren't yet measurable.
- **Action:** capture declined-bar prob (emit it on the vote/diagnostics even when
  below threshold) so separation is computable. v2 retrain is a *refinement*, not a
  rescue (see `docs/ENTRY_MODEL_V2_SPEC.md`).

## STEP 2 — Direction model — DEGENERATE this run (the weak component)
- **Finding:** ce_prob is **flat at ~0.515 across all 119 fired bars** (min 0.5147,
  max 0.5155, **spread 0.0008**); **0 PE signalled** all day → all 10 taken were CE.
- **Evidence:** digest `direction.all_bars`: spread 0.0008, would_be_pe 0, degenerate=true.
- **Reasoning:** model (`direction_only_model.joblib`) is barely responding to inputs,
  sitting a hair above the 0.5 CE/PE cutoff → resolves CE every bar. Consistent with
  "PE prints on other days" (it's pinned just above 0.5, tips under on other data).
  This is the real meaning of "direction model is not good" = near-constant output.
- **Action:** direction-model rework is the priority after entry-v2. Verify across
  more days; quantify with the same `all_bars` degenerate flag.

## STEP 3 — Entry blocking is done by the REGIME gate, not the ML
- **Finding:** of 109 blocked (fired-but-not-taken... see note), primary blockers:
  `sideways_returns_mixed` **85**, `min_reentry_gap` 15, `confidence_gate` 8,
  `candidate_ranking` 1.
- **Reasoning:** 78% of blocking is one regime check
  ([:818-822](../strategy_app/engines/deterministic_rule_engine.py#L818)) — SIDEWAYS +
  mixed returns → no conviction → skip. The ML entry model adds ~no filtering at its
  threshold; the **regime tagger is the real entry filter.**
- **Implication:** whether blocking is *correct* now depends entirely on the regime
  tagger being right → STEP 5.

## STEP 4 — Direction-quality grader is BYPASSED in consensus mode
- **Finding:** `grade_coverage 0/10` — the GOOD/OK/BAD grader ran on zero taken trades.
- **Reasoning:** `grade_entry_from_raw` only understands composite (`entry_dir_*`) or
  consensus-margin (`direction_consensus_*`) keys; consensus mode emits
  `ml_direction_*` → grader returns None ([deterministic_rule_engine.py:657-661](../strategy_app/engines/deterministic_rule_engine.py#L657)).
  So the thin-margin / chop / iv-skew **direction vetoes never evaluate** in the mode
  live runs. Only the conservative evidence gate (vetoes CE only if bear>0.6 & bull<0.2)
  is active → weak CE in chop passes.
- **Action:** make the grader understand `ml_direction_*` (so GOOD/OK/BAD + tier apply
  in consensus mode), OR tighten the evidence gate. See
  [entry_quality.py](../strategy_app/signals/entry_quality.py).

---

## PENDING STEPS
- **STEP 5 — Regime tagger** (drives 78% of blocking): over the full session, was
  SIDEWAYS/TRENDING/BREAKOUT correct vs realised price structure (cross-check against
  the `market_structure` block we capture)? Original 3-loss finding: it mislabeled a
  range-bound tape as TRENDING.
- **STEP 6 — Exits**: exit-reason distribution; are exits premature (the +5-min
  time-stop / MFE-giveback pattern from the earlier 3-loss run)?
- **STEP 7 — Declined-prob capture** (from Step 1 caveat) to measure entry separation.

---

## CONSOLIDATED ACTIONS (what / where) — updated each step
| # | Action | Where | Priority |
|---|--------|-------|----------|
| A1 | Capture declined-bar entry prob (<threshold) | `strategy_app/engines/strategies/ml_entry.py` + engine no-vote trace | High (needed for separation) |
| A2 | Trace the ~9 early-return exit bars (100% coverage) | `deterministic_rule_engine.py:445-446` | Med |
| A3 | Direction-model rework (degenerate, flat 0.515) | `direction_only` model + training | High |
| A4 | Grader to understand consensus `ml_direction_*` (or tighten evidence gate) | `signals/entry_quality.py`, engine evidence gate | High |
| A5 | Entry model v2 retrain (refinement) | `docs/ENTRY_MODEL_V2_SPEC.md` (in progress on ML VM) | Med |

> Note on "109 blocked": that count is over *fired* bars in the pre-fix digest. With
> full-population traces, re-derive blocked counts over all 351 bars in STEP 5.

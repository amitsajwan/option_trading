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

## STEP 3 — Filtering is TWO-STAGE (entry model declines most; regime gate catches the rest) [CORRECTED w/ full population]
- **Finding (full 351-bar population):** `no_strategy_votes` (entry model declined,
  prob<0.65) **198**, `sideways_returns_mixed` **85**, `no_exit_trigger` 34 (position
  mgmt), `min_reentry_gap` 15, `confidence_gate` 8, `candidate_ranking` 1, taken/None 10.
- **Reasoning:** the entry model is the **first and largest** filter — it declines 198
  of ~341 non-position bars (~58%). Of the 119 it *passes*, the regime gate
  ([:818-822](../strategy_app/engines/deterministic_rule_engine.py#L818)) blocks 85
  (71%). So **both** matter — my earlier "regime gate does 78% of blocking" was over
  the fired-bars-only subset (85/109) and undercounted the entry model's own
  declines. Corrected: entry-declines 198 > regime-block 85.
- **Implication:** whether the 85 regime blocks are *correct* depends on the regime
  tagger → STEP 5.

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

## STEP 5 — Regime tagger looks BROADLY CORRECT on this (range-bound) day
- **Finding:** over 351 bars — CHOP 135 (38%), SIDEWAYS 135 (38%), AVOID 65 (19%),
  BREAKOUT 9 (3%), **TRENDING only 7 (2%)**.
- **Reasoning:** 76% chop/sideways for a genuinely range-bound day is appropriate, and
  **the feared "false TRENDING" does NOT reproduce here** (only 2% TRENDING). Cross-tab
  vs swing structure: CHOP/SIDEWAYS bars are mostly `range` (75 / 56) with some micro
  up/down-trend (noisy 1-pivot fractal) — expected divergence between a broad regime
  label and a micro swing read. So on this run the regime tagger is not the culprit;
  it correctly refused a no-trend day.
- **Caveat:** the original mislabel finding was on a *different* day; needs a
  multi-day check before clearing the tagger generally. `AVOID` (65 bars) is a
  no-entry regime (likely open/close windows) — confirm it's time-based, not eating
  tradeable bars.
- **Action:** re-run S5 across several days; spot-check any TRENDING/BREAKOUT bar's
  card against realised forward move.

## STEP 6 — Exits: time-stop dominates, MFE giveback, and exit reason is too coarse
- **Finding:** all 10 trades exit via generic `exit_stack`; hold times cluster ~5 min
  (1,4,6,5,5,5,5,3,5,5). MFE giveback: CE 54900 hit **MFE +2.08%** then exited −0.36%;
  CE 54800 +0.60%→−2.53%. Deep losers ran the full ~5 min (mae −5.04%, −3.00%) with no
  fast stop. Session 4W/6L, net +0.80% (winners +11.6% vs losers −10.8%).
- **Reasoning (two issues):**
  1. **Observability:** `exit_reason="exit_stack"` is the umbrella policy name — we
     cannot see WHICH rule (target/stop/trailing/time/thesis) actually fired. The exit
     cascade is not surfaced like the entry cascade. The specific trigger likely exists
     in `exit_policy_triggered` but isn't propagated to the trace/digest.
  2. **Behavior:** trailing (act 1% / trail 0.5%) and target (4%) appear not to bind —
     everything resolves at the ~5-min time horizon. No effective MFE lock (gave back
     +2.08%) and no fast loss-cut. Matches the prior 3-loss / MFE-giveback finding.
- **Action:** (a) surface the specific exit trigger in the position trace + digest;
  (b) review why trailing/stop don't fire within the 5-min window (params vs the model
  re-eval horizon). See `strategy_app/position/` exit policy + tracker.

### S6 root-cause (CE 54900 +2.08%→−0.36% giveback) — investigated
- **Mechanism:** `tracker.update` ([tracker.py:86-96](../strategy_app/position/tracker.py#L86))
  computes `current_premium` from the bar **close**, sets `pnl_pct`, then
  `mfe_pct = max(mfe_pct, pnl_pct)`. So MFE is **close-based**, and the exit stack is
  checked once per 1-min bar. Trailing locks at `mfe − 0.5%` = +1.58% and fires when a
  *later* bar's pnl drops below that ([exit_policy.py:77-82](../strategy_app/position/exit_policy.py#L77)).
- **What happened:** premium closed **+2.08%** one minute and **−0.36%** the next — a
  ~2.4% round-trip **in a single bar**. Trailing fired on the −0.36% bar (one bar late),
  jumping past the +1.58% lock. So trailing is *not broken* — it executed, but bar-close
  granularity means an intra-minute round-trip exits at the next close.
- **Recorded as** generic `exit_stack`, hiding that it was `TRAILING_STOP` → A6.
- **Refined action (A7):** classify givebacks as **1-bar** (granularity-limited;
  unavoidable at 1-min, accept or go finer-grained) vs **multi-bar** (trailing genuinely
  too loose → tighten). Don't blindly tune the trail.

## STEP 7 — Declined-prob capture (defined; pending implementation)
- **What:** record the entry model's probability on bars where it **declined**
  (prob < 0.65), not just where it fired. Today `ml_entry.py:179` returns `None` below
  threshold and discards the prob, so only the 119 fired probs are visible; the 232
  declined probs are invisible.
- **Why:** to measure **separation** — do fired bars actually have better forward moves
  than declined bars? `verify_entry_label` needs *both* populations. Without declined
  probs, separation is unmeasurable (we can't tell if the 34% fire rate is skill or an
  arbitrary cutoff). It is the exact full-population check we used to expose the
  direction model as degenerate.
- **Where:** emit the computed prob on the no-vote bar's trace `model_diagnostics`
  (`ml_entry.py` + the engine no-vote trace), so the digest sees prob on all ~351 bars.

## Multi-day (pending)
- Re-run S5 (regime) and S6 (exits) across several days to confirm beyond one day.

---

## Using THIS harness to validate a NEW entry model (replacement playbook)
The whole point of the trace tooling: when the entry model is swapped, re-run the
ops-sim and the digest tells you immediately whether the new model is better.
1. Back up the current bundle; drop the new `entry_only_model.joblib` in
   `ml_pipeline_2/artifacts/entry_only/published/` (keep a `.bak`).
2. Ensure it's in the running containers (rebuild/redeploy), re-run ops-sim for the day.
3. Read the digest: **fire rate** (vs the old 34%), entry-prob distribution, and —
   once S7 lands — the **separation** (fired-vs-declined move rate). Compare per-bar
   cards for any behaviour change. Direction model is unchanged (still the known weak
   link), so expect direction to stay flat — judge the entry model in isolation.

---
## STEP 8 — v2 entry model validated via the harness (sim A/B, 2026-06-04)
Compared deployed **E6** vs published **v2 `010pct`** (sim-only override
`ENTRY_ML_MODEL_PATH`/`ENTRY_ML_MIN_PROB`; live untouched). Separation on v2's
**native** target (5-min, 54 pts ≈ 0.10% @ 54k), threshold sweep; base move-rate ≈ 0.42.

| thr | E6 fire/prec/SEP | v2 fire/prec/SEP |
|---|---|---|
| 0.65 | 119 / .462 / n/a (1 declined) | 98 / .459 / +0.04 |
| 0.75 | 105 / .476 / +0.14 | 48 / .479 / +0.05 |
| 0.85 | 58 / .50 / +0.08 | 14 / .571 / +0.14 |
| **0.90** | 16 / .563 / +0.12 | 11 / **.727** / **+0.30** |

- **v2 is structurally better:** prob spread 0.47→1.0 (vs E6 flat 0.59→0.98); its top
  bucket discriminates (0.90: 72.7% prec vs 42% base, +0.30) where E6 can't (56%).
- **E6 is near-constant-yes among *evaluated* bars** (119/120 fire at 0.65) — note the
  earlier "33% fire" used the wrong denominator (353 incl. AVOID + in-position bars
  where the entry model is never consulted); correct denominator is ~120 evaluated.
- **v2's 0.50 op-point does NOT transfer to 2026:** fires 99% at 0.50 here (probs
  shifted high vs the 2024 holdout's 22%). Needs re-tuning to ~0.85–0.90 on live-like
  data before any cut-over.
- **One low-edge day (~42% base move-rate) — multi-day validation required.**
- **Harness gap found:** the digest's separation is hardcoded to 0.65 / 10min-50pts;
  it should match each model's native label + sweep thresholds (done here inline).
- **Actions:** A8 re-tune v2 threshold on 2026 sim data (multi-day); A9 make the digest
  separation params model-aware (label + threshold sweep), not hardcoded.

### S8 correction — ENTRY IS THE FIRST GATE (firing ≠ trading)
The "v2 fires 99% at 0.50 → threshold doesn't transfer / re-tune to 0.85" worry was
**over-stated**: fire-rate-in-isolation is not the operational metric — the downstream
gates (regime/reentry/confidence) cut candidates to a book regardless. Full-cascade A/B
(same day): **E6 took 10 trades (net +0.80%); v2@0.50 took 8** — the same set minus the
bars v2 declined:
- v2 declined **10:33** (E6's −3.0% entry) and instead entered 10:34 (−0.83%): **+2.2%**
- v2 declined **14:19** (E6's −1.83% loser): **+1.8%**
- v2 declined **13:28** (a +0.47% small winner): −0.5%
- net **+0.80% → +4.33%** on the day.
So v2's *few* declines landed on E6's **worst** entries → it improves entry quality at
the margin **even at 0.50**, because the gates handle volume and v2's wider prob
distribution makes its declines targeted. Re-tuning the threshold may not be necessary;
the right metric is **taken-book quality**, not entry fire rate. Caveats: 1 day, ~10
trades, dropped a small winner too, 10:34 gain partly timing-luck → multi-day still required.

### S8 multi-day A/B (E6 vs v2 010pct @0.50, 6 days) — A8
| Day | E6 trades/net | v2 trades/net |
|---|---|---|
| 05-26 | 1 / **−12.14%** | **0 / 0.00%** (sat out) |
| 05-27 | 4 / +0.85% | 1 / +0.33% |
| 06-01 | 2 / +0.74% | 1 / −1.49% |
| 06-02 | 5 / −6.83% | 5 / −6.83% (identical) |
| 06-03 | 5 / +38.31% | 5 / +35.29% |
| 06-04 | 10 / +0.80% | 8 / +4.32% |
| **Σ** | **+21.73%** | **+31.62%** |

- **v2 wins (+9.9% over 6 days), but the edge is TAIL-RISK AVOIDANCE, not alpha:** its
  whole relative advantage is sitting out the −12.14% day (05-26). Ex the +38% outlier
  (06-03): E6 **−16.6%** vs v2 **−3.7%** — almost all the avoided −12% day.
- On high-activity days (06-02, 06-03) **v2 ≈ E6** (same/near trades; gates dominate,
  v2 doesn't prune). v2 differs only on low-activity days.
- **Caveats:** 6 days, 0–10 trades/day, two outlier days (−12% / +38%) dominate. Not
  statistically robust; 06-03 +38% & 06-02 exact tie warrant a sanity check (expiry/gap
  or data artifact). Directionally: v2 ≥ E6, clearly better on tail risk → reasonable
  cut-over candidate at 0.50, pending more days.

---
**Document status: COMPLETE for the 2026-06-04 run.** S0–S6 verified with root causes;
final consolidated verdict + prioritized plan above. S7 + multi-day are tracked actions.

---

## CONSOLIDATED ACTIONS (what / where) — updated each step
| # | Action | Where | Priority |
|---|--------|-------|----------|
| A1 | Capture declined-bar entry prob (<threshold) | `strategy_app/engines/strategies/ml_entry.py` + engine no-vote trace | High (needed for separation) |
| A2 | Trace the ~9 early-return exit bars (100% coverage) | `deterministic_rule_engine.py:445-446` | Med |
| A3 | Direction-model rework (degenerate, flat 0.515) | `direction_only` model + training | High |
| A4 | Grader to understand consensus `ml_direction_*` (or tighten evidence gate) | `signals/entry_quality.py`, engine evidence gate | High |
| A5 | Entry model v2 retrain (refinement) | `docs/ENTRY_MODEL_V2_SPEC.md` (in progress on ML VM) | Med |
| A6 | Surface specific exit trigger (target/stop/trailing/time/thesis) in trace+digest | `strategy_app/position/tracker.py`, `trace_digest.py` | High |
| A7 | Fix MFE giveback — trailing/stop don't bind in the ~5-min window | `strategy_app/position/` exit policy params vs model horizon | High |

> Note on "109 blocked": that count is over *fired* bars in the pre-fix digest. With
> full-population traces, re-derive blocked counts over all 351 bars in STEP 5.

---

# FINAL CONSOLIDATED ANALYSIS (2026-06-04 run)

## Bottom line
**No execution bug / wrong trade was found.** The engine did what it was configured to
do — verified now that the trace is complete and trustworthy. The session was
4W/6L, **net +0.80%**. What we found are **capability weaknesses and observability
gaps**, not malfunctions.

## What is WORKING
- **Entry model** — discriminates (fires 33.9%, declines 66%). The earlier "fires
  100%/useless" was a survivorship-bias artifact of the incomplete trace, now corrected.
- **Regime tagger** — broadly correct on this range-bound day (76% chop/sideways, only
  2% TRENDING; no false-trend). *Single day — needs multi-day confirmation.*
- **Entry discipline gates** — the regime "no-conviction" block (85 bars) is doing
  sensible work refusing chop.
- **Trailing exit** — not broken; it executes (the giveback was a 1-bar granularity
  limit, not a logic fault).

## What is WEAK (real, actionable)
1. **Direction model is degenerate** — flat ~0.515 all day, spread 0.0008, **zero PE
   signalled** → defaults CE every bar. No real side-selection skill. *This is the
   single biggest capability gap.* (Affects every trade.)
2. **Direction-quality grader is bypassed** in consensus mode (the live mode) → the
   thin-margin/chop/iv-skew vetoes never run; only an extreme-evidence guard is active.
3. **Exit observability + intra-bar giveback** — exits recorded generically as
   `exit_stack`; can't see target/stop/trailing/time/thesis. The +2.08%→−0.36% giveback
   is a 1-min round-trip the bar-close trailing can't catch.

## What was NOISE (don't chase)
- "Entry fires 100%" → my analysis error (survivorship bias). Fixed.
- "All CE / no veto" → explained by the flat direction model + grader bypass, not a bug.
- "Regime mislabels TRENDING" → did **not** reproduce this day.

## Prioritized plan (what / where)
| Pri | Action | Where |
|-----|--------|-------|
| **P1** | **Direction model rework** — degenerate/flat; this gates every trade's edge | `direction_only` model + training (after entry-v2) |
| **P1** | Surface the specific exit trigger (not generic `exit_stack`) | `strategy_app/position/tracker.py` → trace → `trace_digest.py` (A6) |
| **P2** | Make grader work in consensus mode (or tighten evidence gate) | `strategy_app/signals/entry_quality.py` + engine evidence gate (A4) |
| **P2** | Capture declined-bar entry probs (true separation) | `ml_entry.py` + engine no-vote trace (A1, S7) |
| **P2** | Classify 1-bar vs multi-bar givebacks before tuning trail | exit-policy review (A7) |
| **P3** | Entry model v2 retrain — refinement, not rescue | `docs/ENTRY_MODEL_V2_SPEC.md` (on ML VM) |
| **P3** | Trace the ~9 early-return exit bars (100% coverage) | `deterministic_rule_engine.py:445` (A2) |
| **P3** | Multi-day re-run of S5/S6 to confirm beyond one day | ops-sim + analyzer |

## One-line verdict
*The plumbing now tells the truth; the strategy isn't buggy; the **direction model is
the real problem**, and exits + grader need observability/coverage before tuning.*

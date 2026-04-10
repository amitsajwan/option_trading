# Intraday Profit Execution Plan

This document is the working execution plan for the BankNifty intraday trading system effort.

Use it as:

- the product goal statement
- the instruction sheet for the team
- the story tracker for the remaining work
- the definition of when this workstream is done or stopped

This is intentionally different from the MIDDAY handover.

- [`midday_recovery_handover.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/midday_recovery_handover.md) explains what happened
- this document defines what we do next and how we decide when to stop

## Product goal

We are building:

- a profitable BankNifty intraday trading system

The business goal is:

- generate repeatable positive net returns after cost
- maintain acceptable drawdown and trade count
- produce a policy simple enough to operate

The business goal is not:

- to prove a MIDDAY-only system
- to keep CE and PE perfectly balanced
- to maximize model complexity

`MIDDAY` is the current research wedge, not the product mission.

## Current reality

These points are now established from completed runs.

1. The current release-path candidate is:
- Stage 1 entry gate
- Stage 2 direction gate
- fixed `L3` or `L6`
- Stage 12 confidence-selection policy

2. The market itself is not the main source of skew in the current MIDDAY wedge.
- raw oracle direction mix is roughly balanced in both windows: ~51% CE validation, ~48% CE holdout
- Stage 1 also remains roughly balanced: ~51% CE validation, ~48% CE holdout

3. Stage 2 is the distortion source — and the distortion is regime amplification, not just CE failure.
- In validation (May-Jul 2024): Stage 2 actionable was 65% CE on a ~51% CE oracle
- In holdout (Aug-Oct 2024): Stage 2 actionable was 17% CE on a ~48% CE oracle
- Stage 2 amplified CE in validation and amplified PE in holdout
- The oracle was roughly balanced both times
- This is not a model that detects direction — it is a model that follows the dominant regime in its training window and projects it forward

4. PE-only is a research benchmark, not a release candidate.
- The top-25% PE-heavy holdout subsets produce PF > 2 with positive net return
- This is a real signal: PE-side Stage 2 pattern has genuine edge on this historical period
- It is not a product: a PE-only book has no protection against regime change
- The team should not treat PE holdout economics as a success criterion for release
- PE-only may be approved under a narrow, separately scoped mandate only — it is not in scope here

5. The following work has already been tried and should not be treated as open questions anymore.
- broad Stage 3 policy-path search
- Stage1+Stage2 counterfactual analysis
- confidence-execution fraction selection
- side-capped execution policy search
- symmetric Stage 2 threshold sweeps
- dual-side CE/PE fraction selection
- asymmetric Stage 2 threshold sweeps
- manual Stage 2 override through full confidence-selection stack

6. None of the above has produced a stable publishable candidate.

## Working interpretation

The current working interpretation, updated after Story 1 closed:

Stage 2 is not simply "bad at CE."
Stage 2 is a regime amplifier.

It learned the directional dominance of its training window and projects it forward. When validation was CE-heavy it amplified CE. When holdout was PE-heavy it amplified PE. Because the oracle was ~50/50 both times, neither outcome reflects genuine directional intelligence.

A regime amplifier will:
- look good in any single historical backtest where the regime is stable
- fail when the regime changes
- cannot be fixed by adjusting thresholds, side caps, or fractions

The correct standard for a directional product is:
- not "was PE profitable in Aug-Oct 2024?"
- but "does the system have direction intelligence that adapts correctly when future conditions differ from training?"

This is now the only hypothesis worth spending engineering effort on in this workstream.

## Delivery rule

From this point onward, work is allowed only if it reduces one of these two uncertainties:

- can Stage 2 be redesigned to produce regime-robust directional selection — meaning its CE/PE output tracks the oracle distribution across changing conditions, not amplifies the training regime?
- if not, is this workstream commercially non-viable and ready to stop?

If a task does not answer one of those questions, it is out of scope.

Specifically: tasks that improve PE-only economics, reduce PE threshold, or optimize single-window backtest performance are out of scope unless separately approved.

## Success criteria

This workstream is successful only if it produces a candidate that is:

- positive on validation and holdout
- above minimum profit factor
- above minimum trade count
- within acceptable drawdown
- operationally simple enough to publish

The exact thresholds can be adjusted by product decision, but they must be frozen before a final proof cycle starts.

## Stop criteria

This workstream should stop if any of the following becomes true:

- one final bounded Stage 2 redesign cycle still fails to produce a credible candidate
- validation remains structurally negative across the best realistic candidates
- the best candidate requires fragile post-hoc tuning or overly narrow conditions
- the team cannot explain a concrete path from model behavior to production rules

## Current status

Status summary:

- upstream edge exists
- broad execution remains weak
- Stage 2 is the main bottleneck
- threshold and wrapper work are now mostly exhausted
- next work must be Stage 2 redesign, not more wrapper tuning

## Story tracker

Status values:

- `TODO`
- `IN_PROGRESS`
- `DONE`
- `STOPPED`

### Story 0: Freeze the operating baseline

Status: `DONE`

Goal:

- freeze the facts already learned so the team stops reopening solved questions

Required outputs:

- completed handover document
- completed skew diagnostic
- completed Stage 2 calibration diagnostic
- completed dual-side policy diagnostic
- completed Stage 2 side-rebalance diagnostic

Done means:

- the team agrees that Stage 2 is the current bottleneck
- no new work is proposed on Stage 3-first or side-cap-first redesign unless new evidence appears

### Story 1: Verify Stage 12 under manual Stage 2 overrides

Status: `DONE`

Goal:

- test the two best asymmetric Stage 2 candidates through the full Stage 12 confidence-policy stack

Candidates tested:

- candidate A: `trade=0.55, ce=0.60, pe=0.65, edge=0.00`
- candidate B: `trade=0.55, ce=0.55, pe=0.65, edge=0.00`

Results:

- holdout top-fraction subsets: PF 2.0–2.4, but `long_share = 0.0` (pure PE)
- CE precision on holdout across all candidates: 15–19% (below 50/50 random baseline)
- when side cap is applied to force balance: book collapses to near-zero trades
- validation economics: negative across all configurations
- no configuration passed hard gates on both windows simultaneously

Outcome: `FAILED`

Threshold tuning is formally closed.
The shared Stage 2 direction model has no CE predictive power on holdout regardless of threshold choice.

The PE book has genuine edge (holdout PF > 2). The CE book is noise at the current Stage 2 model level.

### Story 2: Design the Stage 2 redesign brief

Status: `TODO`

Goal:

- define one focused Stage 2 redesign path whose explicit objective is **directional robustness under regime change** — not CE repair in isolation

The redesign brief must answer all of the following before Story 3 starts:

- why does current Stage 2 behave as a regime amplifier rather than a direction detector?
- what structural change will produce a model whose CE/PE output tracks oracle distribution across regime shifts rather than amplifying the training-window regime?
- how will we test for regime robustness, not just single-window performance?
- what are pass/fail criteria that are independent of which regime happens to dominate a given test window?

Allowed redesign directions:

- regime-conditional Stage 2 features: add regime-state inputs so the model predicts direction conditional on current market state, not just the historical dominant pattern
- multi-window training and validation: train and validate Stage 2 across 3+ distinct windows so that a regime-overfit model is detectable and rejectable before holdout
- directional abstention inside Stage 2: allow Stage 2 to abstain from direction assignment on low-confidence rows instead of defaulting to the currently dominant side

Not approved in this story:

- CE repair that does not simultaneously test PE behavior across regime shifts
- new Stage 3 redesign branch
- full all-session expansion
- optimization of PE-only or single-window metrics

Required tasks:

- write the redesign hypothesis in one paragraph: what is the root cause and what structural change addresses it?
- state the test that would have caught the current regime amplifier before it reached holdout
- define success criteria that are regime-robust (see below)
- select one option and name one owner
- produce one bounded run plan for Story 3

Done means:

- one approved redesign brief exists with a named option and named owner
- the success criteria explicitly include regime-robustness checks

**Draft redesign brief (PM approval required before Story 3 starts):**

Root cause:

Stage 2 trains a shared direction model on one training window and learns the directional dominance of that window. When the market regime shifts, Stage 2 inverts its output — not because it detected the shift, but because it lost its training pattern and defaulted to the new dominant pattern. This is regime amplification, not directional intelligence.

Evidence:

- validation oracle 51% CE → Stage 2 actionable 65% CE — amplified CE by 14pp in a balanced oracle
- holdout oracle 48% CE → Stage 2 actionable 17% CE — amplified PE by 31pp in a balanced oracle
- direction agreement vs oracle: 37–38% on validation, ~48% on holdout — both near or below random
- CE precision holdout: 15–19% on a 48% CE base rate (random = 48%)
- PE precision holdout: 55–76% — PE signal is real but regime-specific, not regime-robust

The model did not learn direction. It learned "which side is currently winning" and projects it forward.

Proposed options (PM selects one):

Option A — Regime-conditioned Stage 2 features:
- Add explicit regime-state inputs to Stage 2 training: e.g. rolling directional win rate, recent CE/PE outcome balance, trend strength, volatility regime indicator
- These features give the model information about current regime so it can condition its direction output on state rather than fitting one historical pattern
- Test: on a held-out window, does Stage 2 CE/PE proportion stay within ±15pp of oracle proportion? If regime features are working, direction output should track oracle mix, not amplify one side

Option B — Multi-window cross-validation for regime robustness:
- Validate Stage 2 across 3+ non-overlapping windows during training
- Reject any model where CE/PE gap vs oracle exceeds 20pp on any single window
- This makes regime overfit detectable before holdout rather than discovered after
- Does not require new features — requires a stricter evaluation contract

Option C — Directional abstention in Stage 2:
- Currently Stage 2 always assigns CE or PE when the trade gate fires
- Add an explicit low-confidence abstention class: the model can output "trade, direction unknown"
- Abstained rows are excluded from the direction-sensitive book, reducing regime amplification without requiring regime features
- Test: abstention rate ≥ 30% on ambiguous rows; directional accuracy on non-abstained rows ≥ 55% across both windows

**Regime-robust success criteria (apply to all options, not negotiable):**

A redesigned Stage 2 passes Story 2 only if on a fresh run with the same windows ALL hold:

- CE/PE gap vs oracle ≤ 15pp on holdout (current: 31pp)
- CE/PE gap vs oracle ≤ 15pp on validation (current: 14pp)
- Direction agreement vs oracle ≥ 50% on holdout (current: ~48%)
- CE precision on holdout Stage 1-positive: ≥ 40% (current: 15–19%)
- PE precision on holdout: ≥ 50% (must not degrade)
- All criteria must hold without PE-only subsetting, side caps, or post-hoc threshold tuning

Economics are explicitly excluded from Stage 2 pass/fail. Stage 2 success means directional robustness. Economics are evaluated separately in Story 3 post-run diagnostics.

### Story 3: Execute one final Stage 2 redesign cycle

Status: `TODO`

Goal:

- run one bounded redesign cycle that directly attacks the Stage 2 bottleneck

Required tasks:

- implement the approved redesign
- run the new staged batch
- run post-run diagnostics:
  - skew diagnostic
  - confidence execution
  - confidence execution policy

Done means:

- we have one fresh redesign result with the same evaluation framework as the current baseline

### Story 4: Final proof assessment

Status: `TODO`

Goal:

- decide whether the redesigned system is publishable

Required tasks:

- compare the redesigned candidate against the frozen baseline
- evaluate against the fixed success criteria
- write one decision memo:
  - `GO`
  - `NO_GO`
  - `STOP`

Done means:

- the team has made an explicit decision and documented it

### Story 5: Product decision

Status: `TODO`

Goal:

- close the workstream with a business decision, not an engineering continuation loop

Possible outcomes:

- publish the candidate
- stop the MIDDAY wedge and archive it
- restart under a broader intraday mandate with a fresh scope and budget

Done means:

- there is no ambiguous “keep researching for now” state left

## Execution order

The stories must be closed in this order:

1. Story 1
2. Story 2
3. Story 3
4. Story 4
5. Story 5

No new parallel research branch should be opened before Story 1 is closed.

## Story board

| Story | Title | Status | Primary output |
| --- | --- | --- | --- |
| S0 | Freeze the operating baseline | DONE | Current facts frozen |
| S1 | Verify Stage 12 under manual Stage 2 overrides | DONE | Both candidates failed — threshold tuning closed |
| S2 | Design the Stage 2 redesign brief | TODO | One approved regime-robustness redesign brief |
| S3 | Execute one final Stage 2 redesign cycle | TODO | One redesign batch result |
| S4 | Final proof assessment | TODO | Go/no-go memo |
| S5 | Product decision | TODO | Publish, stop, or restart decision |

## Team instructions

From now on, team members should follow these rules.

1. Do not present MIDDAY as the business goal.
- MIDDAY is the current narrow wedge only.

2. Do not open new branches of work unless they directly test the Stage 2 bottleneck.

3. Every update must answer:
- what hypothesis was tested
- what changed
- whether confidence in shipping increased or decreased

4. Every run must end with one of:
- keep candidate
- reject candidate
- stop branch

5. Do not use vague progress language.
Use:
- `done`
- `failed`
- `blocked`
- `replaced`

## Immediate next action

Story 1 is closed. Both override candidates failed. Threshold tuning is formally exhausted.

The root cause has been updated: Stage 2 is a **regime amplifier**, not a direction detector. The objective for redesign is now directional robustness, not CE repair.

The immediate next action is:

- PM reviews the three redesign options in Story 2 and selects one
- PM names one owner for Story 3
- do not start Story 3 until the brief is approved and the regime-robust success criteria are accepted

The three options differ in implementation cost but share the same regime-robustness success criteria. Option B (multi-window validation) is the lowest implementation cost and would have caught the current regime amplifier before holdout if it had been applied earlier.

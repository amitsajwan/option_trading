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
- raw oracle direction mix is roughly balanced
- Stage 1 also remains roughly balanced

3. The main distortion appears in Stage 2.
- holdout direction mix becomes heavily PE-skewed after Stage 2 filtering
- ranking amplifies that later, but ranking is secondary

4. The following work has already been tried and should not be treated as open questions anymore.
- broad Stage 3 policy-path search
- Stage1+Stage2 counterfactual analysis
- confidence-execution fraction selection
- side-capped execution policy search
- symmetric Stage 2 threshold sweeps
- dual-side CE/PE fraction selection
- asymmetric Stage 2 threshold sweeps

5. None of the above has yet produced a stable publishable candidate.

## Working interpretation

The current working hypothesis is:

- the MIDDAY wedge is being blocked mainly by Stage 2 directional behavior, especially CE-side quality and CE/PE transfer stability

This is now the only hypothesis worth spending meaningful engineering effort on in this workstream.

## Delivery rule

From this point onward, work is allowed only if it reduces one of these uncertainties:

- can Stage 2 be redesigned so validation and holdout both support a profitable downstream policy?
- if not, is the MIDDAY wedge commercially non-viable and ready to stop?

If a task does not answer one of those questions, it is out of scope.

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

- define one focused Stage 2 redesign path instead of multiple open-ended branches

Allowed redesign directions:

- CE/PE-specific calibration redesign
- CE/PE-specific target redesign
- side-specific confidence modeling inside Stage 2

Not allowed in this story:

- new Stage 3 redesign branch
- full all-session expansion
- broad feature churn without a testable hypothesis

Required tasks:

- write the redesign hypothesis
- state why current Stage 2 fails
- define exactly what changes in Stage 2
- define how success will be measured

Done means:

- there is one approved redesign brief with one owner and one bounded run plan

Draft redesign brief (to be reviewed and approved before Story 3 starts):

**Why current Stage 2 fails:**

The current Stage 2 trains one shared direction model (`CE vs PE`) on the full actionable set. The model has learned PE patterns that generalize to holdout, but CE patterns that do not. Evidence:

- CE precision on holdout: 15–19% across all threshold configurations tested (random baseline on 48% CE oracle is ~48%)
- Stage 2 direction agreement vs oracle is 37–38% on validation, ~46–50% on holdout — both near or below random
- PE precision on holdout is 55–76%, i.e. PE signal is real; CE signal is noise
- The problem is not thresholds — it is the model itself

This is consistent with one or both of:

- CE and PE having structurally different feature patterns that a shared model cannot capture simultaneously
- CE-side training signal being too weak or too sparse to produce a useful direction model on this dataset

**What should change:**

One of the following, in order of implementation cost:

Option A — Side-specific Stage 2 scoring: train two Stage 2 models, one CE-specific and one PE-specific, each predicting "is this a good trade for this side?" rather than "which side is better?". Stage 2 policy then uses CE-model score to gate CE entries and PE-model score to gate PE entries independently.

Option B — CE/PE-specific calibration with separate isotonic layers: keep the shared model but fit separate calibration layers per side using platt or isotonic regression, then use side-specific thresholds with these recalibrated scores.

Option C — CE target redesign only: keep PE model fixed (it works), redesign the CE Stage 2 target to include CE-specific features or a stronger CE signal. Only rebuild the CE half.

**How success is measured:**

A redesigned Stage 2 is viable if, on a fresh research run with the same windows:

- CE precision on holdout Stage 1-positive subset: ≥ 40%
- PE precision on holdout: does not degrade below current 55%
- Stage 1+2 actionable holdout book: ≥ 50 trades at long_share ≥ 0.25
- Validation economics for the confidence-selected book: net_return ≥ 0 at some fraction ≥ top-25%

These targets are intentionally conservative — they do not require publishability, only that the CE signal has become real enough to warrant a full execution policy test.

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
| S2 | Design the Stage 2 redesign brief | TODO | One approved redesign brief |
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

Story 1 is closed. Both override candidates failed.

The immediate next action is:

- review and approve the Stage 2 redesign brief in Story 2
- select one redesign option (A, B, or C) with one owner
- do not start Story 3 until the brief is signed off

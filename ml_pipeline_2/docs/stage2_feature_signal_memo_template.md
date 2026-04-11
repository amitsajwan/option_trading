# Stage 2 Feature Signal Memo Template

> Historical research note. `S2` is closed. Do not use this as the current operating instruction. Use `intraday_profit_execution_plan.md` for current status.

This was the memo template used to close `S2`.

This is a gate memo, not a research note.
It must end with one binary decision:

- `YES`: retraining is justified
- `NO`: retraining is not justified with the current feature set

## Header

- Story: `S2 - Stage 2 feature signal analysis`
- Run id:
- Run directory:
- Analyst:
- Date:
- Status: `DRAFT` / `FINAL`

## Question

Do the current Stage 2 direction features contain enough cross-window directional signal to justify one bounded retraining cycle?

## Comparison Set

The main direction test must use only:

- rows where `entry_label = 1`
- rows where oracle direction is `CE` or `PE`
- validation window and holdout window reported separately

If any other comparison set is shown, label it as supporting evidence only.

## Inputs Used

- Stage 2 model package:
- Stage 2 feature columns:
- Validation window:
- Holdout window:
- Entry threshold used, if any:

## Required Evidence

### 1. Direction Model Snapshot

- model family:
- number of direction features:
- top 5 weighted features:
- what those top 5 appear to measure:

### 2. Feature Separation

Report for validation, holdout, and combined:

- features with meaningful CE vs PE separation
- effect size used:
- significance test used:
- minimum threshold used to call a feature separating:

### 3. Cross-Window Stability

For each separating feature, state:

- sign on validation
- sign on holdout
- whether the sign is stable across both windows

### 4. Regime-Drift Read

State whether the most important direction features drift materially between validation and holdout, and how that affects confidence.

## Findings

### Established Facts

- 

### Current Interpretation

- 

## Decision

Choose exactly one:

### YES - Retraining Justified

Use this only if:

- there is clear cross-window feature separation
- the direction features are not purely regime proxies
- there is a concrete bounded redesign brief for Story 3

Required statement:

`The current feature set contains enough cross-window directional signal to justify one bounded Stage 2 retraining cycle.`

Then list:

- which features justify that decision
- what the Story 3 redesign will change
- what will stay fixed

### NO - Retraining Not Justified

Use this if:

- cross-window separation is weak or unstable
- the strongest features appear to be regime proxies only
- the current feature set does not support regime-robust direction selection

Required statement:

`The current feature set does not contain enough cross-window directional signal to justify retraining as-is.`

Then list:

- whether the right next move is `new features`, `new target`, or `stop`
- whether Story 3 should be skipped

## Recommended Next Action

Choose one:

- `Proceed to Story 3`
- `Return with a new feature brief`
- `Stop the current wedge`

## Sign-Off

- CORE reviewer:
- PM acknowledgement:

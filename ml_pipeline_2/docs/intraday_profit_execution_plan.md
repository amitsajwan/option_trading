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

- S0, S1, S2 are all closed
- PE-side edge is confirmed real: holdout PF > 2 on top subsets
- Stage 2 is confirmed as the bottleneck: regime amplifier, not a direction detector
- S2 feature signal analysis returned NO: 0 cross-window stable features at d≥0.10 out of 24
- Several top features flip sign between windows — they track regime dominance, not direction
- Weak consistent signal exists in ema slopes and OI ratio features, but insufficient alone
- All threshold and wrapper approaches are exhausted
- Next work is a feature brief for S3 — not a retrain, not more tuning

## Story tracker

Status values: `TODO` / `IN_PROGRESS` / `DONE` / `STOPPED`

Team assignments:
- `CORE` — us: architecture decisions, feature analysis, Stage 2 redesign, final assessment
- `TEAM` — delegated: data prep, infrastructure, run execution support, report formatting

---

### Story 0: Freeze the operating baseline

Status: `DONE` | Owner: `CORE`

Goal: freeze facts already learned so the team stops reopening solved questions.

Outputs delivered:

- handover document updated
- skew diagnostic: Stage 2 confirmed as skew source
- Stage 2 calibration diagnostic: symmetric thresholds exhausted
- dual-side policy diagnostic: side-separated fractions do not fix upstream CE weakness
- Stage 2 side-rebalance diagnostic: asymmetric thresholds do not fix CE precision

---

### Story 1: Verify Stage 12 under manual Stage 2 overrides

Status: `DONE` | Owner: `CORE`

Goal: test the two best asymmetric Stage 2 candidates through the full Stage 12 confidence-policy stack.

Candidates tested:

- candidate A: `trade=0.55, ce=0.60, pe=0.65, edge=0.00`
- candidate B: `trade=0.55, ce=0.55, pe=0.65, edge=0.00`

Results:

- holdout top-fraction subsets: PF 2.0–2.4, but `long_share = 0.0` (pure PE)
- CE precision on holdout: 15–19% against a 48% CE random baseline
- side cap applied: book collapses to near-zero trades
- validation economics: negative across all configurations
- no configuration passed hard gates on both windows

Outcome: `FAILED` — threshold tuning is formally closed.

Key finding: Stage 2 is a regime amplifier. It learned the dominant side of the training window and projects it forward. This is not a threshold problem. The features themselves may not contain regime-agnostic direction signal.

---

### Story 2: Stage 2 feature signal analysis

Status: `DONE` | Owner: `CORE`

This was the gate story. It is now closed.

Goal: answer one question before spending compute on a retrain — do the current Stage 2 features contain any regime-agnostic directional signal?

This is a 2–4 hour EDA task, not a training run.

Required tasks (`CORE`):

- pull Stage 2 feature matrix from the existing run artifacts
- split rows by oracle direction label (CE rows vs PE rows)
- for each feature, test separation between CE-oracle and PE-oracle rows — on validation window, on holdout window, and combined
- check: do any features that separate CE/PE on validation still separate on holdout?
- check: is the current Stage 2 target label (best-net-return side) correlated with any features across both windows, or orthogonal?
- check: what are the top-5 Stage 2 feature importances and what do they represent (momentum, regime proxy, fundamental direction)?

Required output:

- one short decision memo: YES signal exists / NO signal does not exist
- if YES: list the features with cross-window separation and characterise what they measure
- if NO: state clearly that the current feature set cannot support a directional product on this data

Result: **NO**

Memo: `analysis/stage2_feature_signal_diagnostic/stage2_feature_signal_memo.md` on the run dir.

Key findings from the memo:

- Direction model: LogisticRegression, 24 features
- Comparison set: 16,074 validation oracle-positive rows, 15,209 holdout oracle-positive rows
- Cross-window stable features at Cohen's d ≥ 0.10 both windows, same sign: **0 out of 24**
- Weak consistent signal exists in a small cluster at d ≈ 0.03–0.08: `ema_21_slope`, `ema_50_slope`, `near_atm_oi_ratio`, `atm_oi_ratio`, `vix_current` — but effect sizes are too small to build a reliable direction model on
- Several top-weighted features **flip sign** across windows: `pcr`, `atm_pe_oi`, `iv_skew`, `atm_pe_iv`, `iv_percentile`, `dist_from_day_high` — these are regime followers, not direction predictors
- Heavy regime drift confirmed in `atm_ce_iv`, `near_atm_oi_ratio`, `atm_oi_ratio`, `vix_current` — the features themselves shift significantly between windows

What this means:

- The current 24-feature set is predominantly composed of regime-correlated inputs, not stable directional predictors
- Retraining the same model on the same features will reproduce regime amplification
- The weak signal cluster (ema slopes, OI ratios) is a starting anchor — it is not zero signal, but it is insufficient alone
- The fix requires **new features**, not new model parameters

Story 3 path: this NO result means vanilla retraining is not justified. However, it does NOT mean the direction prediction problem is unsolvable. It means the feature brief must change before a new training cycle starts. Story 3 is now redesigned around a feature brief, not a model brief.

---

### Story 3: Feature brief and one bounded redesign cycle

Status: `IN_PROGRESS` | Owner: `CORE` (feature brief + review) + `TEAM` (implementation + run)

Unblocked by: Story 2 returning NO — this does not skip to Story 5. It redirects Story 3 to a feature redesign brief first.

Background from S2:

The current 24-feature set has 0 cross-window stable directional features at d≥0.10. Several features flip sign between windows. This means a new training cycle on the same features will fail for the same reason. The fix must start with the features, not the model.

Weak signal anchors from S2 (these exist but are insufficient alone):

- `ema_21_slope`, `ema_50_slope` — consistent direction across windows, small effect
- `near_atm_oi_ratio`, `atm_oi_ratio` — consistent direction, moderate holdout effect, mild validation effect
- `vix_current` — consistent direction, strengthens on holdout

#### Feature brief (APPROVED — CORE)

**Mechanism**: Stage 2 was amplifying regime because it had no memory of recent directional outcomes. Rolling oracle win-rate features give Stage 2 explicit context: "in the last 5–10 days, CE-optimal trades outnumbered PE-optimal trades X% of the time." A model that sees this context can predict direction conditionally on recent regime rather than projecting the training-window regime blindly.

**DROP** (confirmed regime-followers — flip sign between windows):
- `pcr`, `pcr_change_5m`, `pcr_change_15m`, `pcr_oi`, `opt_flow_pcr_oi`
- `iv_skew`, `atm_pe_iv`, `iv_percentile`
- `dist_from_day_*`, `atm_ce_iv`
- `ce_pe_oi_diff`, `opt_flow_ce_pe_oi_diff`

**KEEP** (weak anchors — consistent sign, small but non-zero d):
- `ema_21_slope`, `ema_50_slope`
- `near_atm_oi_ratio`, `atm_oi_ratio`
- `vix_current`

**ADD** (new feature class — rolling oracle win-rates):
- `oracle_rolling_ce_win_rate_5d` — share of CE entries in prior 5 trade-days
- `oracle_rolling_pe_win_rate_5d` — share of PE entries in prior 5 trade-days
- `ce_pe_win_rate_diff_5d` — CE minus PE (positive = CE-dominant regime last 5d)
- `oracle_rolling_ce_win_rate_10d` — same over 10 trade-days
- `oracle_rolling_pe_win_rate_10d`
- `ce_pe_win_rate_diff_10d`

**ADD** (regime binary flags — already computed, add to Stage 2 input):
- `ctx_regime_atr_high`, `ctx_regime_atr_low`, `ctx_regime_trend_up`, `ctx_regime_trend_down`
- `ctx_is_high_vix_day`, `regime_vol_high`, `regime_vol_low`, `regime_trend_up`, `regime_trend_down`

New feature set name: `fo_midday_direction_regime_v1`

#### Implementation status

Code complete — all three changes committed to branch `chore/ml-pipeline-ubuntu-gcp-runbook`:

| Change | File | Status |
|--------|------|--------|
| `compute_rolling_oracle_stats()` function | `staged/pipeline.py` | DONE |
| Stage 2 frame enrichment (inject rolling stats before labeler) | `staged/pipeline.py` | DONE |
| `fo_midday_direction_regime_v1` feature set registration | `catalog/feature_sets.py` | DONE |
| New grid config with Stage 1 frozen | `configs/research/staged_grid.stage3_direction_regime_v1.json` | DONE |
| Pre-flight gate script | `scripts/run_s3_preflight_gate.py` | DONE |

#### GCP runbook (`TEAM` — run in this order)

**Step 1 — Pull latest code on GCP**
```bash
git pull origin chore/ml-pipeline-ubuntu-gcp-runbook
pip install -e ml_pipeline_2/  # if package not yet installed
```

**Step 2 — Run pre-flight gate (fast, ~5 min)**
```bash
cd /path/to/repo
python ml_pipeline_2/scripts/run_s3_preflight_gate.py
```
- Exit 0 = PASS — proceed to Step 3
- Exit 1 = FAIL — stop, report back to CORE with output

**Step 3 — Run full S3 grid (only if Step 2 passes)**
```bash
python -m ml_pipeline_2.run_staged_grid \
  ml_pipeline_2/configs/research/staged_grid.stage3_direction_regime_v1.json
```
Artifacts land in: `ml_pipeline_2/artifacts/research/staged_grid_stage3_direction_regime_v1/`

**Step 4 — Run S2 feature signal diagnostic on the new run**
```bash
python -m ml_pipeline_2.staged.stage2_feature_signal \
  --run-dir ml_pipeline_2/artifacts/research/staged_grid_stage3_direction_regime_v1/runs/01_s3_regime_baseline \
  --output /tmp/s3_feature_signal.json
cat /tmp/s3_feature_signal.json | python -c "import json,sys; d=json.load(sys.stdin); print('STABLE:', d['n_cross_window_stable_features'], '/', len(d['cross_window_stability'])); print('VERDICT:', d['verdict'])"
```

**Step 5 — Run S2 skew diagnostic**
```bash
python -m ml_pipeline_2.staged.stage2_calibration_diagnostic \
  --run-dir ml_pipeline_2/artifacts/research/staged_grid_stage3_direction_regime_v1/runs/01_s3_regime_baseline
```

**Step 6 — Run confidence execution policy**
```bash
python -m ml_pipeline_2.run_stage12_confidence_execution_policy \
  --run-dir ml_pipeline_2/artifacts/research/staged_grid_stage3_direction_regime_v1/runs/01_s3_regime_baseline
```

**Step 7 — Deliver results to CORE**
Paste stdout from Steps 4–6 into the team channel. CORE (Amit) assesses against publish gates.

Required tasks (`CORE`):

- review redesign results against regime-robust success criteria
- write S4 decision memo

Required tasks (`TEAM`):

- run Steps 1–7 of the GCP runbook above
- deliver raw results and auto-generated memos back to CORE

Success criteria (regime-robust, non-negotiable):

- Cross-window stable directional features: ≥ 3 at d ≥ 0.10 both windows (upgrades S2 memo from 0)
- CE/PE gap vs oracle ≤ 15pp on holdout (current: 31pp)
- CE/PE gap vs oracle ≤ 15pp on validation (current: 14pp)
- Direction agreement vs oracle ≥ 50% on holdout (current: ~48%)
- CE precision on holdout ≥ 40% (current: 15–19%)
- All criteria without side caps or post-hoc threshold tuning

⚠️ **Known deferred scope (S4 dependency)**: `oracle_rolling_*` features are training-time features computed from historical oracle. For live `ml_pure` inference, a daily lookup table of rolling oracle stats must be pre-computed and supplied. This is out of scope for S3. It must be resolved before production hand-off.

Done means:

- feature brief is approved (DONE — above)
- implementation is committed (DONE — above)
- GCP run completes with full diagnostics (TEAM — pending)
- CORE reviews results against all regime-robust criteria and makes a keep/reject call

---

### Story 4: Final proof assessment

Status: `TODO` | Owner: `CORE`

Blocked on: Story 3.

Goal: decide whether the redesigned system is publishable.

Tasks (`CORE`):

- compare redesigned candidate against the frozen baseline on all standard metrics
- check regime-robust criteria from Story 2
- run full confidence execution policy on the redesigned candidate
- write one decision memo: `GO` / `NO_GO` / `STOP`

A GO requires: positive on both windows, minimum profit factor, minimum trade count, acceptable drawdown, side balance within gates, no fragile post-hoc tuning.

A STOP is issued if: direction balance criteria still fail, or the team cannot explain the model's direction logic in plain language.

Done means: one signed decision memo exists.

---

### Story 5: Product decision

Status: `TODO` | Owner: `PM + CORE`

Goal: close the workstream with a business decision.

Possible outcomes:

- `PUBLISH`: candidate meets all criteria, hand off to release pipeline
- `STOP MIDDAY`: archive this wedge with full documentation, no further research spend
- `RESTART BROADER`: stop MIDDAY, restart under a wider intraday mandate with more data, more regimes, fresh scope

Done means: no "keep researching for now" state remains. A decision is recorded and acted on.

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

Stories must close in this sequence. No skipping, no parallel research branches.

1. S0 — done
2. S1 — done
3. S2 — feature signal analysis gate (CORE, ~2-4 hours)
4. S3 — bounded redesign cycle (CORE spec + TEAM execution), only if S2 returns YES
5. S4 — final proof assessment (CORE)
6. S5 — product decision (PM + CORE)

If S2 returns NO signal: skip S3 and S4 entirely, go directly to S5 with a STOP recommendation.

## Story board

| Story | Title | Status | Owner | Gate |
| --- | --- | --- | --- | --- |
| S0 | Freeze the operating baseline | DONE | CORE | — |
| S1 | Verify Stage 12 under manual Stage 2 overrides | DONE | CORE | — |
| S2 | Stage 2 feature signal analysis | DONE | CORE | Result: NO — 0 stable features, feature brief required |
| S3 | Feature brief + one bounded redesign cycle | TODO | CORE+TEAM | Needs PM-approved feature brief |
| S4 | Final proof assessment | TODO | CORE | Needs S3 |
| S5 | Product decision | TODO | PM+CORE | Needs S4 |

## Team instructions

**For CORE team:**

1. Own S2 directly. Do not delegate the feature analysis. The decision on whether to retrain at all depends on what you find there.
2. If S2 returns YES, write a tight spec for TEAM before handing off S3 execution. TEAM should not be deciding what to change — only implementing what is specified.
3. Every story update must state: hypothesis tested, result, and confidence in shipping increased or decreased.
4. Every story ends with one word: `done`, `failed`, `blocked`, or `stopped`. No vague progress language.

**For TEAM:**

1. S2 is CORE-only. Your job in S2 is to ensure data and artifact access.
2. In S3 your job is implementation and run execution per the CORE spec. Do not extend the scope — implement exactly what is specified.
3. Do not open new feature branches or model variants without CORE sign-off.
4. Deliver raw results and diagnostics. Do not interpret or make keep/reject calls. That is CORE's job.

**For everyone:**

- MIDDAY is a research wedge, not the product mission
- PE-only results are diagnostic benchmarks, not release candidates
- do not present single-window improvements as progress unless regime-robust criteria also pass

## Immediate next action

S0, S1, and S2 are closed.

S2 returned **NO**: 0 cross-window stable directional features. Vanilla retraining is not justified.

**The immediate next action is: CORE writes the feature brief for S3.**

The brief must specify:
- which regime-state features to add (candidates: rolling CE win rate, directional consistency score, trend regime indicator, recent intraday drift)
- which confirmed regime-follower features to remove (candidates: `pcr`, `atm_pe_iv`, `iv_skew`, `iv_percentile`, `dist_from_day_high`)
- the mechanism: why the new features will produce stable direction signal rather than regime following
- PM sign-off before TEAM implementation starts

The weak signal anchors confirmed in S2 — `ema_21_slope`, `ema_50_slope`, `near_atm_oi_ratio`, `atm_oi_ratio`, `vix_current` — should be kept and supplemented, not discarded.

This is a CORE design task. It is not an implementation task. It should take half a day, not a week.

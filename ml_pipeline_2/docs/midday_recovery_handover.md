# MIDDAY Recovery Handover

This is the onboarding and handover document for the staged MIDDAY recovery work in `ml_pipeline_2`.

It is written for a new engineer or researcher who needs to understand:

- the problem statement
- the staged architecture we are using
- the recovery path we followed
- what is solved
- what is still unsolved
- how to restart the work from scratch without replaying every dead end

Use this document first.
Then read the linked stage-specific docs only for the area you need to work on.

## Problem statement

The target system is a staged options-trading model for the `ml_pure` lane.

In this recovery track we focused on the `MIDDAY` slice and tried to answer one practical question:

- can we build a publishable staged model that:
  - chooses tradable MIDDAY opportunities
  - picks the correct direction
  - chooses an execution recipe
  - remains economically positive on holdout after costs

Publishability here is not just model accuracy.
The staged stack must also clear downstream trade and risk gates such as:

- positive `net_return_sum`
- adequate `profit_factor`
- enough trades
- acceptable drawdown
- acceptable side balance

The core lesson from this track is:

- Stage 2 probability quality was a real blocker first
- after fixing that, the remaining blocker moved downstream into execution economics and side concentration

## Short architecture

This recovery work uses the staged pipeline in `ml_pipeline_2`.

Very short version:

1. Stage 1
- broad entry gate
- "should this row even be considered?"

2. Stage 2
- directional decision inside Stage 1 positives
- after redesign, this became hierarchical:
  - trade vs no-trade
  - then `CE vs PE`

3. Stage 3
- execution recipe selection
- horizon / TP / SL style choice
- in practice this controls how directional edge is monetized

Downstream evaluation then checks:

- combined holdout economics
- publish gates
- robustness and selection behavior

## Code map

These are the main files a new joiner should know.

Core staged runtime and research logic:

- [`pipeline.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py)
- [`grid.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/grid.py)
- [`registries.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/registries.py)
- [`recipes.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/recipes.py)
- [`runtime_contract.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/runtime_contract.py)

Entry points:

- [`run_staged_grid.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/run_staged_grid.py)
- [`run_staged_release.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/run_staged_release.py)

Post-run analysis tools added during recovery:

- [`run_stage12_counterfactual.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/run_stage12_counterfactual.py)
- [`counterfactual.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/counterfactual.py)
- [`run_stage12_confidence_execution.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/run_stage12_confidence_execution.py)
- [`confidence_execution.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/confidence_execution.py)
- [`run_stage12_confidence_execution_policy.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/run_stage12_confidence_execution_policy.py)
- [`confidence_execution_policy.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/confidence_execution_policy.py)
- [`run_stage12_skew_diagnostic.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/run_stage12_skew_diagnostic.py)
- [`skew_diagnostic.py`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/src/ml_pipeline_2/staged/skew_diagnostic.py)

Current research configs:

- [`staged_dual_recipe.stage3_policy_paths_v1.json`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/configs/research/staged_dual_recipe.stage3_policy_paths_v1.json)
- [`staged_grid.stage3_midday_policy_paths_v1.json`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json)

## Recovery path we followed

This is the useful sequence.
It intentionally omits low-value churn and keeps only the steps that changed the diagnosis.

### 1. Stage 2 MIDDAY redesign

Doc:

- [`stage2_midday_redesign.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_redesign.md)

Why we did it:

- the original Stage 2 problem was noisy and not stable enough
- MIDDAY looked like the most promising slice to simplify

What it gave us:

- a cleaner Stage 2 problem boundary
- a tighter research focus

### 2. Stage 2 target redesign

Doc:

- [`stage2_midday_target_redesign.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_target_redesign.md)

Why we did it:

- many rows were ambiguous for directional learning
- forcing them into a binary direction label hurt calibration

What it gave us:

- cleaner directional labels
- better control over what Stage 2 should learn

### 3. Stage 2 high-conviction attempts

Doc:

- [`stage2_midday_high_conviction.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_high_conviction.md)

Why we did it:

- to check whether simple threshold tightening was enough

What we learned:

- useful negative evidence
- threshold tuning alone was not enough
- the Stage 2 formulation itself needed to change

### 4. Stage 2 direction-or-no-trade redesign

Doc:

- [`stage2_midday_direction_or_no_trade.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_direction_or_no_trade.md)

Why we did it:

- old Stage 2 forced `CE vs PE`
- that overloaded direction with abstention logic

What changed:

- Stage 2 became hierarchical:
  - `trade_gate`
  - conditional direction

What we gained:

- Stage 2 CV became genuinely strong again
- representative winning Stage 2 metrics:
  - `roc_auc ~= 0.68`
  - `brier ~= 0.205`

This is the main upstream success of the recovery work.

### 5. Stage 3 policy-path recovery

Doc:

- [`stage3_midday_policy_paths.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage3_midday_policy_paths.md)

Why we did it:

- after Stage 2 was fixed, the blocker moved downstream
- the question became whether Stage 3 selection and policy logic were the main reason for bad holdout economics

What changed:

- Stage 2 reuse support
- fixed-recipe fallback support
- broader Stage 3 policy-path search
- modest recipe expansion

What we learned:

- dynamic Stage 3 selection was weak
- fixed fallback behaved more sensibly
- the best fallback recipe was still not publishable on the broad trade set

Representative result:

- winning broad fixed fallback:
  - `285` trades
  - `net_return_sum ~= -0.072`
  - `profit_factor ~= 0.706`

So Stage 3 policy logic improved, but broad execution economics still failed.

### 6. Stage1+Stage2 counterfactual analysis

Doc:

- [`stage12_counterfactual.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_counterfactual.md)

Why we did it:

- to separate two hypotheses:
  - "the upstream model has no edge"
  - "the upstream model has edge, but execution is leaking it"

What we learned:

- Stage 1 + Stage 2 do contain real edge
- broad execution is too weak
- higher-confidence subsets improve sharply

Representative holdout results:

- full set `285` trades:
  - fixed `L3`: negative
  - fixed `L6`: negative
  - oracle selected-side: slightly positive
- top `50%`, `143` trades:
  - fixed `L3`: positive
  - fixed `L6`: positive
- top `33%`, `95` trades:
  - fixed `L3`: positive
  - fixed `L6`: positive
- top `25%`, `72` trades:
  - fixed `L3`: positive
  - fixed `L6`: positive

This was the second major recovery result:

- the model is not dead
- the broad book is too loose

### 7. Confidence execution

Docs:

- [`stage12_confidence_execution.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_confidence_execution.md)

Why we did it:

- to convert the counterfactual into a validation-driven selector

Important lesson:

- the first version used raw validation score-floor transfer and failed because score scales did not transfer
- the second version fixed this by transferring the selected fraction, not the raw score threshold

What we learned:

- fraction-based transfer reproduces the positive holdout subsets
- but validation itself is still weak, so automated selection is not yet stable enough for release

### 8. Confidence execution policy

Doc:

- [`stage12_confidence_execution_policy.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_confidence_execution_policy.md)

Why we did it:

- the remaining structural issue was heavy one-sided exposure, especially PE-heavy books

What changed:

- fixed `L3` / `L6` only
- tighter fractions only
- optional side caps after confidence ranking

What this batch is for:

- testing whether a side-aware fixed execution policy can turn the promising subset behavior into something closer to a releaseable policy

## Current state

What is solved:

- Stage 1 is acceptable enough to keep fixed during this track
- Stage 2 is materially improved and no longer the main blocker
- the stack has real directional edge on tighter subsets
- fixed execution with tighter subsets can produce positive holdout behavior

What is not solved:

- broad execution still fails economics
- validation-side selection is still weaker than holdout-side behavior
- side balance is still poor, especially on good PE-heavy subsets
- a fully publishable, stable, automatically selected policy is not yet demonstrated

## Current best interpretation

The cleanest current interpretation is:

- the model works on the stronger slice
- the current release blocker is execution-policy stability and side concentration
- not basic Stage 2 quality

That means the work is now in refinement territory, not rescue territory.

## What a new joiner should do first

If you are taking over this track, do this in order.

1. Read this document.
2. Read the top-level recovery sequence:
- [`research_recovery_runbook.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/research_recovery_runbook.md)
3. Read only these stage docs next:
- [`stage2_midday_direction_or_no_trade.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_direction_or_no_trade.md)
- [`stage3_midday_policy_paths.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage3_midday_policy_paths.md)
- [`stage12_counterfactual.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_counterfactual.md)
- [`stage12_confidence_execution.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_confidence_execution.md)
- [`stage12_confidence_execution_policy.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_confidence_execution_policy.md)
4. Open the core code files listed in the code map.
5. Inspect the latest winning run artifacts before changing code.

## From-scratch restart process

This is the shortest useful restart path.

### A. Environment and branch

```bash
cd ~/option_trading
git fetch origin
git checkout chore/ml-pipeline-ubuntu-gcp-runbook
git pull --ff-only origin chore/ml-pipeline-ubuntu-gcp-runbook
git log --oneline -n 10
```

Activate the environment:

```bash
. .venv/bin/activate
```

### B. Confirm required code paths exist

```bash
ls -la ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py
ls -la ml_pipeline_2/src/ml_pipeline_2/staged/counterfactual.py
ls -la ml_pipeline_2/src/ml_pipeline_2/staged/confidence_execution.py
ls -la ml_pipeline_2/src/ml_pipeline_2/staged/confidence_execution_policy.py
```

### C. Run the current main research batch

This is still the base staged research run for this recovery track:

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

### D. Inspect the winner

At minimum inspect:

- `grid_summary.json`
- winner `summary.json`
- `policy_reports`
- `combined_holdout_summary`

### E. Run post-run analysis

Counterfactual:

```bash
python -m ml_pipeline_2.run_stage12_counterfactual \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --top-fractions 1.0 0.5 0.33 0.25 0.1 \
  --fixed-recipes L3 L6
```

Confidence execution:

```bash
python -m ml_pipeline_2.run_stage12_confidence_execution \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --top-fractions 1.0 0.5 0.33 0.25 0.1 \
  --fixed-recipes L3 L6 \
  --transfer-mode fraction \
  --validation-min-trades-soft 50 \
  --side-share-min 0.30 \
  --side-share-max 0.70 \
  --prefer-profit-factor-min 1.0
```

Confidence execution policy:

```bash
python -m ml_pipeline_2.run_stage12_confidence_execution_policy \
  --run-dir ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/runs/03_stage3_balanced_gate_fixed_guard \
  --top-fractions 0.5 0.33 0.25 \
  --fixed-recipes L3 L6 \
  --side-cap-grid 1.0 0.85 0.75 0.70 \
  --validation-min-trades-soft 50 \
  --side-share-min 0.30 \
  --side-share-max 0.70 \
  --prefer-profit-factor-min 1.0
```

## How to interpret the current evidence

Use this logic.

If broad Stage 3 winner is still negative:

- do not reopen Stage 2 by default
- inspect counterfactual first

If counterfactual says higher-confidence subsets improve:

- the upstream edge is real
- continue with fixed execution analysis or side-aware execution policy

If confidence execution works only on holdout but not validation:

- the model may still be useful
- but automated release selection is not stable enough yet

If side-aware execution policy still cannot make validation and holdout agree:

- move to explicit side-aware execution logic
- or Stage 3 target/view redesign

## What we gained

These are the durable gains from this track.

- Stage 2 is no longer the main bottleneck
- a hierarchical Stage 2 is better than forced binary direction
- tighter confidence slices carry real economic edge
- fixed `L3` / `L6` controls are useful and should remain in future analysis
- validation-to-holdout transport by fraction is better than raw score-floor transfer

## What is still missing

These are the real unresolved items.

- a validation-selected policy that is both positive enough and stable enough
- acceptable side balance on the stronger subsets
- a clear release-quality execution policy that does not depend on hindsight-style diagnosis
- if side-aware policy still fails, a cleaner Stage 3 target/view formulation

## Recommended next work

The current recommended order is:

1. Finish evaluating the fixed-execution policy batch on the current winner run.
2. If side-capped fixed execution improves validation without killing holdout:
- productize that as the next research candidate
3. If side caps do not solve the instability:
- move to explicit side-aware execution logic
4. If that still fails:
- redesign Stage 3 target/view rather than reopening Stage 2

## Related docs

Core architecture:

- [`README.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/README.md)
- [`architecture.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/architecture.md)
- [`detailed_design.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/detailed_design.md)

Recovery sequence:

- [`research_recovery_runbook.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/research_recovery_runbook.md)
- [`stage2_midday_redesign.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_redesign.md)
- [`stage2_midday_target_redesign.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_target_redesign.md)
- [`stage2_midday_high_conviction.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_high_conviction.md)
- [`stage2_midday_direction_or_no_trade.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage2_midday_direction_or_no_trade.md)
- [`stage3_midday_policy_paths.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage3_midday_policy_paths.md)
- [`stage12_counterfactual.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_counterfactual.md)
- [`stage12_confidence_execution.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_confidence_execution.md)
- [`stage12_confidence_execution_policy.md`](c:/code/option_trading/option_trading_repo/ml_pipeline_2/docs/stage12_confidence_execution_policy.md)

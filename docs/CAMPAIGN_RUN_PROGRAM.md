# ML Campaign Run Program
> **Status**: Active
> **Last updated**: 2026-04-12
> **Purpose**: Source of truth for how we should run ML campaigns on the current single-VM GCP setup.
> **Scope**: Launch order, search strategy, promotion rules, and operational discipline.

This document is the run-order source of truth.

Use this together with:
- [ML_PIPELINE_FULL_FLOW.md](./ML_PIPELINE_FULL_FLOW.md) for the end-to-end pipeline mechanics
- [STAGE2_DIRECTION_RECOVERY_PLAN.md](./STAGE2_DIRECTION_RECOVERY_PLAN.md) for the Stage 2 research problem
- [SNAPSHOT_ENRICHMENT_SPRINT.md](./SNAPSHOT_ENRICHMENT_SPRINT.md) for data/feature backlog
- [VELOCITY_RETRAIN_WORK_PLAN.md](./VELOCITY_RETRAIN_WORK_PLAN.md) for the broader workstream backlog
- [VELOCITY_DATA_READINESS_AND_LAUNCH.md](./VELOCITY_DATA_READINESS_AND_LAUNCH.md) for the Gate 0 data/view readiness contract

If any older document implies a different launch order, this document wins.

## Gate 0

Velocity campaign launch is blocked until the readiness gate in
[VELOCITY_DATA_READINESS_AND_LAUNCH.md](./VELOCITY_DATA_READINESS_AND_LAUNCH.md)
passes. A velocity screen run is not valid if it mixes `snapshots_ml_flat_v2`
with `v1` stage views.

---

## 1. Core Clarification

There are two different questions:

1. What the platform can support
2. What we should run on the current GCP VM

The platform now supports:
- campaign generation
- workflow orchestration
- lane dependencies
- grid execution
- resume and retry
- broad family-based search

That does **not** mean we should launch every possible search branch at once.

On the current `n2-highmem-8` VM, the right strategy is:

```text
screen -> narrow -> HPO -> exploit
```

Not:

```text
everything -> everywhere -> all at once
```

---

## 2. Why The Search Must Be Hierarchical

The current bottleneck is not platform capability. It is research efficiency and limited compute.

Reasons:
- Stage 2 is still the weakest link, so broad Stage 3 exploration before Stage 2 survivors emerge wastes compute.
- The current VM is effectively single-lane for large campaigns because each lane requests most of the box.
- Launching multiple broad campaigns in parallel makes attribution worse and slows all runs.
- Duplicate campaigns with the same `campaign_id` and `--fresh` are especially bad because they contend for the same artifact root.

The correct hierarchy is:

1. Screen for signal
2. Promote only survivors
3. Spend HPO budget only on survivors
4. Spend Stage 3 policy/catalog budget only on survivors
5. Publish or shadow only after the final economic gate

---

## 3. Non-Negotiable Rules

### One broad campaign at a time
- On the current VM, run only one major campaign at a time.
- Do not launch a second broad search while another screen campaign is still active.

### Search by stages, not giant cartesian products
- Stage 1 already performs internal model and feature search.
- Stage 2 is the current research bottleneck.
- Stage 3 should be explored only after Stage 2 survivors exist.

### Screening before deep HPO
- Broad screens should use cheap HPO or light search.
- Deep Optuna HPO is for top survivors only.

### Explicit output roots
- Always use `--fresh` for reruns of the same campaign id.
- Prefer explicit `--output-root` on GCP so artifact paths are predictable.

### No duplicate campaign launches
- Before relaunching, verify no old `run_training_campaign` or `run_staged_grid` processes remain for the same campaign.

---

## 4. Recommended Run Ladder

## Phase 1 - Broad Screen

### Goal
Find which Stage 2 family is worth believing.

### Current best screen
`velocity_screen_campaign_v1`

Launch note:
- use this only after Gate 0 passes
- it is the correct phase-1 screen **once** the `v2` data/view path is ready

Config:
- [velocity_screen_campaign_v1.json](../ml_pipeline_2/configs/campaign/velocity_screen_campaign_v1.json)
- [staged_grid.velocity_screen_v1.json](../ml_pipeline_2/configs/research/staged_grid.velocity_screen_v1.json)
- [staged_dual_recipe.velocity_screen_v1.json](../ml_pipeline_2/configs/research/staged_dual_recipe.velocity_screen_v1.json)

### What it tests
- 1 window profile: `canonical_4y`
- 1 model family: `full_catalog`
- 4 Stage 2 feature families:
  - `velocity_v1`
  - `regime_v1`
  - `oi_pcr_momentum`
  - `expiry_aware_v3`
- 1 Stage 2 policy family:
  - `economic_balance_gate`
- 3 grid runs per lane:
  - `all_day`
  - `midday_only`
  - `midday_late`

### Internal search inside each lane
- Stage 1: full internal model catalog
- Stage 2: full internal model catalog
- Stage 3: full internal model catalog
- HPO mode: `random`
- Trials per model: `5`

### What this phase is for
- cheap elimination
- relative ranking of Stage 2 families
- deciding what deserves deeper HPO

### What this phase is not for
- final production selection
- deep hyperparameter tuning
- broad Stage 3 policy exploration

---

## Phase 2 - Focused HPO

### Goal
Take the top `1-2` Stage 2 survivors from Phase 1 and tune them properly.

### Recommended base config
- [staged_dual_recipe.velocity_hpo_v1.json](../ml_pipeline_2/configs/research/staged_dual_recipe.velocity_hpo_v1.json)

### HPO profile
- Stage 1: Optuna, `20` trials per model
- Stage 2: Optuna, `30` trials per model
- Stage 3: Optuna, `15` trials per model

### Promotion rule from Phase 1 to Phase 2
Promote only lanes that:
- pass Stage 2 signal check
- have the best combination of:
  - Stage 2 ROC AUC
  - Stage 2 Brier
  - drift stability

### Preferred survivor count
- best case: promote `1`
- acceptable: promote `2`
- do not promote more than `2` on the current VM unless results are nearly tied

### Why this phase is separate
- deep HPO is expensive
- the screen campaign is for breadth
- this phase is for quality on a very small set of candidates

---

## Phase 3 - Stage 3 Exploitation

### Goal
Once Stage 2 has credible survivors, search the Stage 3 policy, recipe, and runtime space that best monetizes them.

### What should vary here
- Stage 3 policy family
- recipe catalog family
- runtime family
- optionally Stage 2 policy family, if the promoted Stage 2 family is still sensitive to gating behavior

### What should not vary here
- broad Stage 2 feature family search again
- broad Stage 1 family search again

### Reason
This phase is for economic exploitation of a direction signal that already survived screening and HPO.

---

## Phase 4 - Publish, Shadow, Or Close The Thread

### Publishable
Only if final holdout and combined economics gates pass.

### Shadow candidate
If close but not publishable, keep for review or shadow testing depending on risk appetite.

### Close the thread
If Phase 2 still cannot produce a stable Stage 2 winner, stop spending compute on wider Stage 3 search.

At that point:
- either pivot to CE-only or entry-only paths
- or declare the current Stage 2 direction thread closed

---

## 5. Decision Rules Between Phases

### After Phase 1 screen

#### If 0 credible survivors
- Do not run deep HPO.
- Reassess feature engineering or pivot strategy.

#### If 1 clear survivor
- Run focused HPO on that single survivor.

#### If 2 close survivors
- Run focused HPO on both.

#### If 3 or more are close
- Rank by:
  1. Stage 2 drift stability
  2. Stage 2 ROC AUC
  3. Stage 2 Brier
  4. combined economics if available
- Promote at most top `2`.

### After Phase 2 HPO

#### If no survivor passes the research bar
- Stop Stage 3 exploitation.
- Consider CE-only or close the Stage 2 direction thread.

#### If 1 survivor is clearly best
- Use that survivor as the base for Stage 3 exploitation.

#### If 2 remain close
- Run a very small Stage 3 exploitation campaign on both, not a broad one.

---

## 6. What The Current Velocity Run Is Actually Doing

The current active `velocity_screen_campaign_v1` run is a **broad screen**.

It is **not**:
- the final HPO pass
- the final Stage 3 policy exploration pass
- a full multi-window, multi-policy, multi-catalog exhaustive campaign

It **is**:
- a real 3-stage training run
- using the enriched `snapshots_ml_flat_v2` dataset
- using broad internal model catalogs
- comparing four different Stage 2 feature families
- running session-path variants inside each lane
- using light HPO for breadth

That is the correct first step.

---

## 7. What We Should Not Do Now

Do not:
- start another broad velocity screen while this one is active
- start broad Stage 3 policy campaigns before the screen finishes
- run broad Optuna HPO across all screen lanes
- run multiple campaigns with the same `campaign_id` on the same VM

All of those reduce clarity and waste compute.

---

## 8. What We Should Do Next

### Immediate
- Let the current `velocity_screen_campaign_v1` finish.
- Do not launch another broad campaign on the same VM.

### As soon as the screen finishes
- extract the per-lane Stage 2 metrics
- rank survivors by stability first, not ROC alone
- choose top `1-2`

### Then
- create a focused HPO campaign or manifests for those `1-2` survivors only

### Then
- run a small Stage 3 exploitation search on the winner or top two

---

## 9. Operational Commands

### Recommended launch pattern on GCP

```bash
cd ~/option_trading
nohup /home/savitasajwan03/option_trading/.venv/bin/python \
  -m ml_pipeline_2.run_training_campaign \
  --spec ml_pipeline_2/configs/campaign/velocity_screen_campaign_v1.json \
  --fresh \
  --output-root /home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/campaign_runs \
  > /home/savitasajwan03/logs/velocity_screen.log 2>&1 &
```

### Process sanity check

```bash
ps aux | grep run_training_campaign | grep -v grep
ps aux | grep run_staged_grid | grep -v grep
```

### Workflow state

```bash
cat ~/option_trading/ml_pipeline_2/artifacts/campaign_runs/velocity_screen_campaign_v1/workflow_state.json
```

### Per-run progress

```bash
tail -20 ~/option_trading/ml_pipeline_2/artifacts/campaign_runs/velocity_screen_campaign_v1/lanes/*/runner_output/runs/*/state.jsonl
```

---

## 10. Recommended Source Of Truth For The Next Month

For run execution decisions:
- this document

For Stage 2 research conclusions:
- [STAGE2_DIRECTION_RECOVERY_PLAN.md](./STAGE2_DIRECTION_RECOVERY_PLAN.md)

For pipeline mechanics:
- [ML_PIPELINE_FULL_FLOW.md](./ML_PIPELINE_FULL_FLOW.md)

For feature backlog:
- [SNAPSHOT_ENRICHMENT_SPRINT.md](./SNAPSHOT_ENRICHMENT_SPRINT.md)

For velocity-specific implementation backlog:
- [VELOCITY_RETRAIN_WORK_PLAN.md](./VELOCITY_RETRAIN_WORK_PLAN.md)

---

## 11. Bottom Line

The platform is capable of broad search.

The right operating model on the current VM is still:

```text
Phase 1: broad screen
Phase 2: focused HPO on top 1-2
Phase 3: Stage 3 exploitation on the winner(s)
Phase 4: publish, shadow, or close the thread
```

That is the disciplined way to get to a publishable result without turning the campaign system into an uncontrolled compute sink.

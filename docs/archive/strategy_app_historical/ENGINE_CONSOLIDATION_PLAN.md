# Engine Consolidation Plan

> **Status as of 2026-04-27: Complete.** This document records the consolidation that was carried out. See `strategy_app/docs/README.md` for the current engine state.

**Date:** 2026-03-19
**Goal:** Keep one research lane (`deterministic`) and one live lane (`ml_pure`), with no runtime ML wrapper layered on top of deterministic rule votes.

## Current State

This plan is no longer a proposal. Most structural consolidation work is already complete in the current tree.

- The legacy transitional runtime wrapper is removed from `strategy_app/main.py`.
- Runtime supports only `deterministic` and `ml_pure`.
- Legacy wrapper modules are removed from the active runtime path.
- Compose/runtime handoff is centered on `ml_pure`.
- Default deterministic routing already excludes `EXPIRY_MAX_PAIN`.
- Deterministic exit handling is owner-first with explicit helper exits and tracker-owned universal mechanics.

Use `strategy_app/docs/CURRENT_TREE_VALIDATION.md` for the code-verified status check behind these statements.

## Why This Plan Exists

The consolidation goal is still valid:

- `deterministic` is the inspectable replay lane used to validate strategy and risk behavior.
- `ml_pure` is the production lane that scores raw features directly.
- there is no supported middle state where an ML wrapper scores deterministic strategy outputs in live runtime.

This split keeps ownership cleaner:

- strategy/risk fixes can be validated in replay without hidden ML coupling
- live ML artifacts are published and switched through a strict staged bundle contract
- runtime behavior is easier to attribute by lane

## Completed Structural Changes

### Runtime lane cleanup

- Removed the legacy transitional runtime wrapper from CLI choices.
- Removed runtime dependency on the old deterministic-vote ML wrapper.
- Standardized live runtime on `ml_pure` artifact resolution by `run_id + model_group` or explicit bundle paths.

### Deterministic engine fixes already landed

- B1: strategy-owned exit priority
- B2: `EXPIRY_MAX_PAIN` removed from default router
- B3: EMA/OI exit-quality fixes
- B4: session rollover and VIX-halt robustness
- B5: confidence-aware budget lot sizing

These are implemented code changes, not pending tasks.

## Remaining Work

What remains is operational hardening, replay validation, and doc hygiene.

### 1. Replay re-baseline on current code

Run deterministic replay on the current snapshot dataset and confirm:

- default `EXPIRY` produces no `EXPIRY_MAX_PAIN` entries
- exit reason mix still looks sensible under owner-first routing
- trade counts and regime slices match expectations for the current router

Reason:

- historical review numbers in older docs are not enough
- the repo snapshot used in this pass did not include the referenced replay artifacts

### 2. Refresh research narratives after replay

After replay completes:

- update portfolio-level metrics in research docs
- keep historical findings only when they can be traced to reproducible artifacts
- avoid presenting old replay tables as current runtime truth

### 3. Retrain and publish `ml_pure` when data or label recipes change

Current staged labels in `ml_pipeline_2` are built from forward futures-path barrier labeling, not deterministic strategy-exit replay. That means:

- deterministic exit fixes do not automatically invalidate staged labels
- retraining is still required whenever the staged feature set, label recipe, history window, or runtime assumptions materially change

Operational sequence:

1. validate staged config and manifest
2. regenerate stage views when upstream data/features change
3. train and publish staged artifacts
4. inspect publish/holdout outputs
5. switch `ml_pure` by approved `run_id + model_group`

### 4. Extend feature-parity assurance

Current state:

- there is focused parity test coverage for core streamable features
- there is no runtime contract that proves full online/batch feature parity

Recommended next step:

- widen parity coverage for the exact staged feature set used by live `ml_pure`

## Lane Ownership

### `deterministic`

- purpose: replay, strategy evaluation, router/risk validation
- not the live production lane

### `ml_pure`

- purpose: production runtime
- consumes published staged artifacts only
- should remain decoupled from deterministic vote outputs

## Final Target State

- `--engine deterministic`: replay and research only
- `--engine ml_pure`: only supported live lane
- legacy transitional runtime wrapper: removed and not reintroduced

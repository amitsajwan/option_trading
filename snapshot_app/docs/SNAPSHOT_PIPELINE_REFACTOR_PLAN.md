# Snapshot Pipeline Refactor Plan

## Status

Design proposal only. No code changes to snapshot production semantics are part of this plan.

## Goal

Refactor the historical snapshot/parquet pipeline into materialized stages so that:

- expensive historical work is not recomputed in one monolithic pass
- partial builds fail closed instead of being published as complete
- downstream contracts remain stable during the first migration
- rebuilds and retries become cheaper and more predictable

## Current problem

The current historical builder is doing too much in one path:

1. normalize raw source files
2. plan day windows and warmup slices
3. build canonical snapshots
4. project ML-flat rows
5. project stage views
6. validate outputs
7. publish outputs

This creates three practical issues:

1. performance
- stateful per-minute snapshot construction, ML-flat projection, and stage-view projection are fused together
- retries repeat work that should already be materialized

2. operational ambiguity
- partial histories can be written and published even when some days were skipped

3. restart cost
- one interrupted or partial run forces re-entry into a large composite pipeline instead of a smaller failed stage

## Current files most affected

### Primary refactor targets

- `snapshot_app/historical/snapshot_batch.py`
- `snapshot_app/historical/snapshot_batch_runner.py`
- `snapshot_app/pipeline/orchestrator.py`
- `snapshot_app/historical/parquet_store.py`
- `ops/gcp/run_snapshot_parquet_pipeline.sh`
- `ops/gcp/publish_snapshot_parquet.sh`

### Projection and contract-adjacent targets

- `snapshot_app/core/stage_views.py`
- `snapshot_app/core/market_snapshot.py`
- `snapshot_app/core/market_snapshot_contract.py`
- `snapshot_app/core/snapshot_ml_flat_contract.py`

### Docs and runbooks

- `docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- `snapshot_app/historical/README.md`

## Proposed target architecture

### Stage 1: Raw archive -> normalized parquet

Purpose:
- convert source CSV/text/archive inputs into normalized per-source parquet

Inputs:
- raw futures, options, spot, VIX archives

Outputs:
- `parquet_data/futures/...`
- `parquet_data/options/...`
- `parquet_data/spot/...`
- `parquet_data/vix/...`

Properties:
- source-specific
- idempotent
- partitioned
- no snapshot semantics

Owner:
- existing normalize flow

### Stage 2: Normalized parquet -> `market_base`

Purpose:
- build the aligned minute-level intermediate dataset once

Inputs:
- normalized futures/options/spot/VIX parquet

Outputs:
- new dataset: `parquet_data/market_base/...`

Minimum contents:
- `trade_date`
- `timestamp`
- futures OHLCV/OI minute row
- spot minute row
- VIX minute row
- chain aggregates
- ATM strike
- total CE/PE OI
- total CE/PE volume
- PCR
- max pain
- ATM option fields needed by downstream snapshot logic

Properties:
- no ML projection
- no stage-view projection
- should be heavily aggregation-oriented
- good candidate for DuckDB/Polars optimization later

Owner:
- new builder module

### Stage 3: `market_base` -> canonical `snapshots`

Purpose:
- run stateful snapshot logic only

Inputs:
- `market_base`

Outputs:
- `parquet_data/snapshots/...`

Properties:
- retains current snapshot semantics
- owns state carrier and minute-by-minute evolution
- should not also emit ML-flat or stage views

Owner:
- snapshot builder path re-scoped to canonical snapshots only

### Stage 4: `snapshots` -> derived research datasets

Purpose:
- pure downstream projection

Inputs:
- canonical `snapshots`

Outputs:
- `parquet_data/snapshots_ml_flat/...`
- `parquet_data/stage1_entry_view/...`
- `parquet_data/stage2_direction_view/...`
- `parquet_data/stage3_recipe_view/...`

Properties:
- no stateful minute replay
- restartable and parallelizable
- should be much cheaper than Stage 3

Owner:
- new projection path using existing contracts and `stage_views`

### Stage 5: Validation and publish

Purpose:
- decide whether output is publishable

Rules:
- publish only if:
  - build status is complete
  - `error_count == 0`
  - `days_skipped_missing_inputs == 0`
  - `days_no_rows == 0`
  - schema validation passes
  - window readiness is acceptable for the intended run class

Owner:
- runner wrapper and GCP publish scripts

## First migration target

Do not do the full redesign in one step.

### Recommended first implementation slice

1. add `market_base`
2. move ML-flat and stage-view projection out of `snapshot_batch.py`
3. tighten publish gating

This gives most of the operational gain without immediately rewriting canonical snapshot logic.

## Compatibility guarantees for phase 1

These outputs must remain schema-compatible:

- `snapshots`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`

Downstream systems expected to remain unchanged in phase 1:

- `ml_pipeline_2`
- `strategy_app`

## Proposed module split

### Existing code to keep

- keep `snapshot_app/core/market_snapshot.py` as canonical snapshot semantics
- keep `snapshot_app/core/market_snapshot_contract.py`
- keep `snapshot_app/core/snapshot_ml_flat_contract.py`
- keep `snapshot_app/core/stage_views.py` as projection logic

### New modules to introduce

- `snapshot_app/historical/market_base_builder.py`
  - builds `market_base`

- `snapshot_app/historical/snapshot_from_market_base.py`
  - consumes `market_base`
  - emits canonical `snapshots`

- `snapshot_app/historical/derived_views_builder.py`
  - consumes `snapshots`
  - emits `snapshots_ml_flat` and stage views

- `snapshot_app/historical/publish_gate.py`
  - centralizes fail-closed publish checks

### Existing modules to shrink

- `snapshot_app/historical/snapshot_batch.py`
  - current giant mixed-responsibility file
  - after refactor, should no longer own all final outputs

- `snapshot_app/historical/snapshot_batch_runner.py`
  - should become a thin CLI dispatcher across stages

- `snapshot_app/pipeline/orchestrator.py`
  - should orchestrate stage-specific jobs, not a monolithic mixed-output batch

## Operational problems this design should fix

### 1. Silent partial publish

Current issue:
- missing-input days can be skipped while the run still reports `complete`

Target:
- any skipped-input day or no-row day blocks publish by default

### 2. Recompute amplification

Current issue:
- historical snapshot rebuild also regenerates all derived research outputs in the same pass

Target:
- canonical snapshots are built once
- derived datasets are projected later from canonical snapshots

### 3. Expensive retries

Current issue:
- failure in late-stage projection can require rerunning expensive earlier work

Target:
- rerun only the failed stage

### 4. Planning from incomplete readiness

Current issue:
- current planning starts from futures-day availability, then skips missing options later

Target:
- planning should use full input readiness for the target stage

## Migration phases

### Phase 0: Guardrails

No output schema changes.

Changes:
- fail publish on partial output
- improve manifest/report visibility for skipped days and missing-input days

Expected impact:
- safer operations immediately

### Phase 1: Introduce `market_base`

Changes:
- add new intermediate dataset
- keep existing final outputs unchanged

Expected impact:
- foundation for splitting expensive work

### Phase 2: Move derived projections out of snapshot batch

Changes:
- canonical snapshot builder emits only `snapshots`
- separate derived builder emits `snapshots_ml_flat` and stage views

Expected impact:
- cheaper reruns
- better parallelism

### Phase 3: Refine orchestration

Changes:
- stage-specific CLI entrypoints
- stage-aware manifests and progress reporting

Expected impact:
- simpler restart model
- better observability

### Phase 4: Performance optimization

Changes:
- optimize `market_base` aggregation using DuckDB/Polars where practical
- tune parallelism per stage

Expected impact:
- faster historical backfills

## What not to do first

### Do not start with Flink

Reason:
- current pain is historical backfill and batch restartability
- Flink adds infrastructure and operational complexity before the dataflow is stabilized

### Do not rewrite snapshot semantics and contracts simultaneously

Reason:
- too much downstream risk
- phase 1 should preserve final output contracts

### Do not change ML/trading consumers in the first pass

Reason:
- the refactor should be isolated to snapshot pipeline shape first

## Acceptance criteria

Phase 1 is successful when:

1. final output schemas remain unchanged for:
- `snapshots`
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`

2. partial histories are not publishable by default

3. rerunning derived view generation does not require rebuilding canonical snapshots

4. build manifests clearly report:
- missing-input days
- no-row days
- error days
- publish readiness

## Open design questions

1. Should `market_base` include full option-chain rows or only pre-aggregated chain state?
- recommendation: only the minimum needed for snapshot construction

2. Should stage-view projection read from `snapshots` only, or from `snapshots_ml_flat`?
- recommendation: derive stage views from canonical `snapshots` or a shared normalized projection layer, not from the live stateful batch loop

3. Should validation run after every stage or only before publish?
- recommendation: lightweight validation after each stage, publish validation only at the end

## Recommendation

Proceed with a small refactor first:

1. add publish fail-closed behavior
2. add `market_base`
3. split `snapshots` generation from derived research projections

That is the best risk-adjusted path to better performance and cleaner operations without destabilizing downstream consumers.

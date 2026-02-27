# Architecture Review AR1 (Post T03)

Date: `2026-02-21`
Scope checkpoint: after T03 (raw quality + canonical dataset done)

## Reviewed Areas

1. Data model boundaries
2. Storage format choices
3. Dataset assembly performance and maintainability

## Findings

1. `raw_loader.py` centralizes raw parsing and symbol normalization, removing duplicated CSV parsing logic.
2. `dataset_builder.py` produces a stable canonical minute panel keyed by futures timestamps.
3. Option neighborhood extraction (`ATM-1`, `ATM`, `ATM+1`) is deterministic and day-stable via inferred strike step.
4. Current per-minute dictionary approach is efficient enough for representative multi-day runs and test loops.

## Decisions

1. Keep canonical panel as the contract between ingestion and feature/label layers.
2. Keep Parquet as artifact format for panel and features.
3. Keep feature/label modules separate from runtime collector/replayer code.

## Refactor Actions

No structural refactor required immediately.

Accepted architectural refinements already applied:

1. Shared raw loader introduced (`raw_loader.py`)
2. Common archive path resolution reused from `schema_validator.py`

## Risks to Revisit in AR2

1. Option outlier handling may need model-aware thresholds (currently generic IQR profiling).
2. If full-history builds become slow, move day-build loop to parallel execution and add artifact partitioning.

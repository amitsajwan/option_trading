# Execution Architecture

## Purpose

The staged training system is split into five layers:

1. manifest resolution
2. execution coordination
3. run execution
4. orchestration and ranking
5. release and publish

Campaign and factory execution sit above the orchestration layer and reuse the same lower-level run and status machinery.

This document describes the execution-facing architecture that keeps long-running research jobs safe, auditable, and reusable across underlyings and date windows.

## Layers

### Manifest Layer

- `ml_pipeline_2/src/ml_pipeline_2/contracts/manifests.py`
- validates staged manifests and staged grid manifests
- declares configuration and policy, but does not control filesystem lifecycle

### Execution Coordination Layer

- `ml_pipeline_2/src/ml_pipeline_2/experiment_control/coordination.py`
- `ml_pipeline_2/src/ml_pipeline_2/experiment_control/registry.py`
- `ml_pipeline_2/src/ml_pipeline_2/experiment_control/status.py`

Responsibilities:

- output-root preparation
- lock acquisition
- reuse semantics
- run and grid status artifacts
- integrity signaling

### Run Execution Layer

- `ml_pipeline_2/src/ml_pipeline_2/experiment_control/runner.py`

Responsibilities:

- validate runtime environment
- create the run context
- emit lifecycle events
- invoke the scenario runner
- finalize run status

### Domain Layer

- `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`
- `ml_pipeline_2/src/ml_pipeline_2/staged/robustness.py`

Responsibilities:

- staged feature/label/model logic
- diagnostics
- robustness probing

This layer must stay independent of root-reuse, lock, and release policy.

### Orchestration Layer

- `ml_pipeline_2/src/ml_pipeline_2/staged/grid.py`
- `ml_pipeline_2/src/ml_pipeline_2/campaign/runner.py`
- `ml_pipeline_2/src/ml_pipeline_2/factory/runner.py`

Responsibilities:

- scenario expansion
- dependency ordering
- lane execution
- result collation
- ranking
- robustness attachment
- workflow-level status collation for campaigns and factories

### Release Layer

- `ml_pipeline_2/src/ml_pipeline_2/staged/publish.py`

Responsibilities:

- release assessment
- publish
- runtime bundle assembly
- GCS sync

Release is integrity-aware and should only publish runs that are structurally complete and execution-safe.

## Status Artifacts

### Lane

Each lane root writes:

- `state.jsonl`
- `run_status.json`
- `summary.json`

`run_status.json` is the authoritative lifecycle file.

Long-running staged runs may also emit setup lifecycle events before any `stage_start` event:

- `prep_start`
- `prep_done`

### Grid

Each grid root writes:

- `grid_status.json`
- `grid_summary.json`
- `manifests/`
- `runs/`

## Reuse Modes

All explicit output-root entrypoints use the same reuse semantics:

- `fail_if_exists`
- `resume`
- `restart`

`fail_if_exists` is the default.

## Integrity Model

Current integrity values:

- `clean`
- `restarted`
- `contaminated`
- `unknown`

The release path currently requires `clean` integrity for publishability.

## Operator Rule

For long research jobs:

- use a deterministic output root
- keep default `fail_if_exists`
- use `restart` only when intentionally discarding a contaminated or abandoned root
- do not manually reuse partially written roots

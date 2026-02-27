# ML Execution Plan (Task-by-Task)

This is the source of truth for model development execution.
Workflow rule for every task:

1. Plan
2. Code
3. Test
4. Review and sign-off

Status legend:
- `[ ]` not started
- `[-]` in progress
- `[x]` completed
- `[!]` blocked

## Scope

Goal: build a production-ready ML timing model for intraday BankNifty option buy/sell decisions.
Out of scope for initial phase: portfolio optimization, advanced multi-leg options strategies, capital allocation optimization.

## Execution Rules

1. Work strictly in task order unless explicitly reprioritized.
2. No task is marked complete without tests and artifact outputs.
3. No model promotion without out-of-sample walk-forward evidence.
4. Architecture review happens on schedule (see below) and may trigger refactor tasks.

## Task Backlog

### Foundation
- `[x] T01` Data Contract and Schema Freeze
  - Output: `docs/data_contract.md`
  - Test: schema validator passes on representative days (different years/expiries)
- `[x] T02` Raw Data Quality Profiler
  - Output: `artifacts/data_quality_report.json` + summary markdown
  - Test: unit tests for missing/duplicate/outlier detection
- `[x] T03` Canonical Minute Dataset Builder
  - Output: normalized panel parquet (futures + spot + options ATM neighborhood)
  - Test: row-count/time-alignment tests across instruments and sessions

### Feature + Label
- `[x] T04` Feature Engineering V1
  - Output: feature table with documented column definitions
  - Test: feature computation unit tests and leakage guard tests
- `[x] T05` Label Engine V1 (trade-aligned)
  - Output: CE/PE labels for fixed holding horizon and optional TP/SL style outcomes
  - Test: forward-window correctness tests and leakage tests

### Modeling
- `[x] T06` Baseline Training Pipeline
  - Output: trained model + metrics report + feature importance
  - Test: deterministic training smoke test and metric computation tests
- `[x] T07` Walk-Forward Validation Pipeline
  - Output: fold-wise metrics and aggregate summary
  - Test: chronological split integrity tests
- `[x] T08` Threshold Optimization
  - Output: chosen operating thresholds and expected-value report
  - Test: threshold search correctness + reproducibility checks

### Trading Simulation
- `[x] T09` Backtest Engine V1
  - Output: event-level trades + PnL summary
  - Test: entry/exit timing tests, no-lookahead tests, cost-application tests
- `[x] T10` Strategy Comparison Harness
  - Output: comparison between CE-only, PE-only, dual-side no-trade policy
  - Test: consistent evaluation dataset and deterministic results

### Deployment Readiness
- `[x] T11` Live Inference Adapter (paper mode)
  - Output: inference service consuming current data pipeline and emitting decisions
  - Test: replay dry-run integration test
- `[x] T12` Monitoring and Drift Checks
  - Output: data drift + prediction drift reports
  - Test: alert trigger tests on synthetic drift scenarios
- `[x] T13` Documentation and Runbook
  - Output: operator runbook, model card, retraining SOP
  - Test: full reproducibility run from clean environment

## Periodic Architecture Reviews

- `[x] AR1` After T03
  - Focus: data model boundaries, storage format, dataset assembly performance
  - Refactor gate: allow package/module reshaping before feature expansion
- `[x] AR2` After T07
  - Focus: model design, validation integrity, leakage risk, experiment tracking
  - Refactor gate: allow API redesign for train/eval pipeline contracts
- `[x] AR3` After T11
  - Focus: live inference architecture, latency, failure handling, observability
  - Refactor gate: allow runtime packaging and deployment structure changes

## Completion Criteria (Phase 1)

Phase 1 is complete when:

1. T01-T13 are completed with passing tests.
2. AR1, AR2, and AR3 reviews are completed.
3. Backtest performance, drift monitoring, and reproducibility assumptions are documented.

## Current Active Task

- `[x] Phase 1 complete`

## Change Log

- 2026-02-21: Initial execution plan created.
- 2026-02-21: T01 completed with schema validator, tests, and representative-day report artifact.
- 2026-02-21: T02 completed with raw-data quality profiler + JSON/MD artifacts.
- 2026-02-21: T03 completed with canonical minute panel builder + parquet artifact.
- 2026-02-21: AR1 completed; data model boundaries accepted (shared raw loader + canonical panel contract).
- 2026-02-21: T04 completed with feature engineering pipeline + leakage tests + feature spec document.
- 2026-02-21: T05 completed with fixed-symbol trade-aligned label engine + tests + labeled parquet/report artifacts.
- 2026-02-21: T06 completed with deterministic CE/PE baseline training, model package, metrics report, and training tests.
- 2026-02-21: T07 completed with day-based walk-forward validation, fold metrics, aggregate summaries, and chronology-integrity tests.
- 2026-02-21: AR2 completed; validation integrity accepted and T08 threshold optimization gated on walk-forward outputs.
- 2026-02-21: T08 completed with walk-forward threshold optimization, deterministic threshold selection tests, and expected-value report artifact.
- 2026-02-21: T09 completed with fold-safe backtest engine, event-level trades, PnL summary report, and timing/no-lookahead/cost tests.
- 2026-02-21: T10 completed with CE-only/PE-only/dual strategy comparison harness, deterministic comparison tests, and cost-sensitivity reporting.
- 2026-02-21: T11 completed with paper-mode live inference adapter (replay dry-run + live-api), decision JSONL output, and replay integration tests.
- 2026-02-21: AR3 completed; live inference architecture and observability path accepted for monitoring/drift stage.
- 2026-02-21: T12 completed with feature/prediction drift monitoring, PSI/action-shift alerting, synthetic drift tests, and drift report/summary artifacts.
- 2026-02-22: T13 completed with operator runbook, model card, retraining SOP, reproducibility runner (double-run deterministic check), and reproducibility report artifacts.

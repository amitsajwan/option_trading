# ML Phase 3 Execution Plan (Task-by-Task)

This is the source of truth for Phase 3 development.
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

Goal: deliver a proper, production-ready BankNifty CE/PE timing model with rigorous validation, controlled overfitting risk, and execution-safe rollout.
Out of scope for this phase: portfolio-level allocation optimization, multi-leg strategy generation as alpha source, discretionary/manual trade overrides.

## Execution Rules

1. Work strictly in task order unless explicitly reprioritized.
2. No task is marked complete without tests and artifact outputs.
3. No model promotion without leakage-safe walk-forward evidence and post-cost robustness checks.
4. No live promotion without shadow-run parity and explicit rollback paths.
5. Architecture review happens on schedule (see below) and may trigger refactor tasks.

## Task Backlog

### Governance and Model Quality
- `[x] T25` Decision Event Contract and Validator
  - Output: strict validator for paper/live decision-event JSONL with schema + semantic checks
  - Test: unit tests for valid/invalid events and deterministic validation report
- `[x] T26` Futures+Options-Only Dataset Freeze
  - Output: documented train/validation/test split with futures+options-only feature profile and dataset lineage report
  - Test: feature-availability parity tests for train/eval/live inputs
- `[x] T27` Label and Horizon Validation (Breakout-Aware)
  - Output: validated base 3-minute labels plus breakout-aware alternative label set
  - Test: forward-window correctness + no-lookahead tests
- `[x] T28` Leakage Audit + Purged Walk-Forward CV
  - Output: leakage audit report and purged/embargoed walk-forward evaluation harness
  - Test: chronology, purge-window, and synthetic leakage-injection tests
- `[x] T29` Model Zoo + Hyperparameter Optimization
  - Output: baseline benchmark (logistic, XGBoost, LightGBM) and tuned champion model
  - Test: deterministic training/tuning tests and search boundary checks
- `[x] T30` Execution Simulator V2 (Latency + Partial Fill)
  - Output: event-driven execution simulator with queue/partial fill behavior
  - Test: latency/fill determinism tests and accounting tests
- `[x] T31` Calibration + Thresholding V2
  - Output: calibrated probability curves (Platt/isotonic comparison) and threshold policy for CE/PE/dual modes
  - Test: Brier/reliability tests and threshold reproducibility checks

### Execution, Risk, and Operations
- `[x] T32` Overfitting/Underfitting Diagnostics + Cost/Slippage Stress
  - Output: train-vs-valid gap report, learning-curve diagnostics, and post-cost robustness matrix
  - Test: synthetic diagnostic tests and sensitivity-accounting tests
- `[x] T33` Order Intent, Shadow Reconciliation, and Runtime Guards
  - Output: idempotent order-intent contract, decision-vs-fill reconciliation report, and enforceable runtime kill-switch/guardrails
  - Test: dedupe, replay safety, mismatch accounting, and guard-trigger tests
- `[x] T34` Documentation Consolidation + Reproducibility V3
  - Output: consolidated operator runbook/model card/retraining SOP and phase3 reproducibility report
  - Test: full clean-room reproducibility run from reset environment

## Periodic Architecture Reviews

- `[x] AR7` After T29
  - Focus: model design, leakage defenses, CV integrity, and tuning governance
  - Refactor gate: allow train/eval API and artifact contract redesign
- `[x] AR8` After T33
  - Focus: execution architecture, idempotency, reconciliation, and runtime safety controls
  - Refactor gate: allow runtime package reshaping before final docs/promotion
- `[x] AR9` After T34
  - Focus: production readiness, failure handling, operational playbooks, rollback rigor, and maintenance simplicity
  - Refactor gate: allow deployment structure changes

## Completion Criteria (Phase 3)

Phase 3 is complete when:

1. T25-T34 are completed with passing tests.
2. AR7-AR9 reviews are completed.
3. Shadow-run, robustness, and reproducibility evidence are documented with rollback-ready operations.

## Current Active Task

- `[x] Phase 3 complete; promotion decision documented (paper/shadow GO, live NO-GO)`

## Change Log

- 2026-02-22: Phase 3 execution plan initialized.
- 2026-02-22: T25 completed with decision-event contract (`decision_event_contract_v1.md`), validator module/tests, and `t25_decision_event_validation_report.json` artifact.
- 2026-02-22: T26 completed with futures+options-only dataset freeze module (`dataset_freeze.py`), split lineage and parity artifacts (`t26_dataset_freeze_report.json`, `t26_dataset_freeze_summary.md`), and unit tests (`test_dataset_freeze.py`).
- 2026-02-22: T27 completed with label/horizon validation module (`label_validation.py`), breakout alternative label artifact (`t27_breakout_alternative_labels.parquet`), validation artifacts (`t27_label_validation_report.json`, `t27_label_validation_summary.md`), and unit tests (`test_label_validation.py`).
- 2026-02-22: T28 completed with purged/embargoed walk-forward support (`walk_forward.py`), leakage audit harness (`leakage_audit.py`), real-data artifacts (`t28_leakage_audit_report.json`, `t28_leakage_audit_summary.md`), and tests (`test_leakage_audit.py`).
- 2026-02-22: T29 completed out-of-order (explicit user reprioritization) with iterative training-cycle harness (`training_cycle.py`), preprocessing gates (missing-rate filter + quantile clipping), model/feature combination leaderboard artifact (`t29_training_cycle_report.json`), best-model package (`t29_best_model.joblib`), and unit tests (`test_training_cycle.py`).
- 2026-02-22: T31 completed with calibration/thresholding v2 harness (`calibration_threshold_v2.py`), method comparison (identity/platt/isotonic), CE/PE/dual threshold policy artifacts (`t31_calibration_threshold_report.json`, `t31_calibration_threshold_summary.md`), and tests (`test_calibration_threshold_v2.py`).
- 2026-02-22: Added automated resumable 2-year real-data pipeline (`train_two_year_pipeline.py`) with day discovery, chunk checkpointing, end-to-end artifact generation, and run outputs (`t29_2y_auto_*`).
- 2026-02-22: T30 completed out-of-order (explicitly reprioritized) with `execution_simulator_v2.py`, deterministic simulator tests, execution simulator spec, and `t30_execution_*` artifacts.
- 2026-02-22: Phase 3 backlog reprioritized to model-quality-first track (futures+options-only dataset freeze, leakage-safe CV, hyperparameter tuning, overfit/underfit diagnostics) while retaining execution-safety milestones.
- 2026-02-22: T32 completed with diagnostics harness (`diagnostics_stress.py`), deterministic tests (`test_diagnostics_stress.py`), and real-data artifacts (`t32_diagnostics_stress_report.json`, `t32_diagnostics_stress_summary.md`).
- 2026-02-22: T33 completed with order-intent/reconciliation runtime module (`order_intent_runtime.py`), tests (`test_order_intent_runtime.py`), and artifacts (`t33_order_runtime_report.json`, `t33_order_runtime_summary.md`, `t33_order_intents.parquet`, `t33_order_fills.parquet`).
- 2026-02-22: T34 completed with phase3 reproducibility runner (`phase3_reproducibility_runner.py`), deterministic tests (`test_phase3_reproducibility_runner.py`), consolidated v2 docs updates, and pass artifacts (`t34_phase3_reproducibility_report.json`, `t34_phase3_reproducibility_summary.md`).
- 2026-02-22: AR7 completed (`architecture_review_ar7.md`) with model/leakage/CV/tuning governance decisions and non-blocking follow-up refactors.
- 2026-02-22: AR8 completed (`architecture_review_ar8.md`) with idempotency/reconciliation/guardrail architecture review and live `NO-GO` guard decision.
- 2026-02-22: AR9 completed (`architecture_review_ar9.md`) with production-readiness review, phase closure, and promotion-gate follow-ups.
- 2026-02-22: Phase 3 completion criteria satisfied (T25-T34 + AR7-AR9 complete); promotion stance recorded as paper/shadow `GO`, live capital `NO-GO` pending non-halt guard stability.

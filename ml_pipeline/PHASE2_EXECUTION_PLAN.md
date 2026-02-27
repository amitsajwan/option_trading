# ML Phase 2 Execution Plan (Task-by-Task)

This is the source of truth for Phase 2 development.
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

Goal: improve trade exit quality and execution realism for intraday BankNifty option decisions.
Out of scope for this phase: portfolio optimization, multi-account allocation, advanced options structures (iron condor/spreads as primary strategy engine).

## Execution Rules

1. Work strictly in task order unless explicitly reprioritized.
2. No task is marked complete without tests and artifact outputs.
3. No production promotion without out-of-sample evidence for net-of-cost behavior.
4. Architecture review happens on schedule (see below) and may trigger refactor tasks.

## Task Backlog

### Exit Engine Foundation
- `[x] T14` Exit Policy Contract and Schema
  - Output: `ml_pipeline/docs/exit_policy_contract_v1.md`
  - Test: parser/validator tests for exit policy configs
- `[x] T15` Label Engine V2 (Path-Aware Outcomes)
  - Output: label columns for TP/SL hit order, time-stop, and hold-extension eligibility
  - Test: forward-path correctness and no-lookahead tests
- `[x] T16` Backtest Engine V2 (Intrabar Exit Simulation)
  - Output: event-level trades with explicit `exit_reason` (`tp`, `sl`, `time`, `trail`, `forced_eod`)
  - Test: deterministic entry/exit sequencing and fee/slippage application tests

### Exit Strategy Layer
- `[x] T17` Dynamic Exit Policy V1
  - Output: configurable trailing-stop / break-even / hold-extension policy module
  - Test: unit tests for policy state transitions and threshold behavior
- `[x] T18` Exit Policy Optimization Harness
  - Output: search report over exit-policy parameter grid
  - Test: deterministic search reproducibility and fold-isolation tests
- `[x] T19` Strategy Comparison V2
  - Output: compare fixed-hold vs dynamic exit policies on identical evaluation sets
  - Test: consistency tests for dataset parity across policies

### Execution Realism
- `[x] T20` Slippage + Fill Model V1
  - Output: configurable fill/slippage model integrated in backtest
  - Test: synthetic spread/volume stress tests and cost-accounting tests
- `[x] T21` Paper Replay Evaluation Harness
  - Output: replay report that maps live-paper decisions to eventual realized exits
  - Test: replay alignment and timestamp integrity tests

### Deployment Readiness
- `[x] T22` Live Inference Adapter V2 (Exit-Aware Paper Mode)
  - Output: paper loop that emits entry + managed exit intents
  - Test: end-to-end replay dry-run with exit state persistence
- `[x] T23` Monitoring V2 (Exit Drift + Execution Quality)
  - Output: drift/health report including exit-reason distribution and hold-duration shifts
  - Test: alert trigger tests on synthetic execution drift scenarios
- `[x] T24` Documentation and Runbook V2
  - Output: updated runbook, model card addendum, retraining SOP addendum for exit-aware system
  - Test: full reproducibility run from clean environment

## Periodic Architecture Reviews

- `[x] AR4` After T16
  - Focus: exit-engine boundaries, label/backtest contract consistency, no-lookahead guarantees
  - Refactor gate: allow module/API reshaping before policy optimization
- `[x] AR5` After T21
  - Focus: execution realism assumptions, replay-to-backtest parity, observability gaps
  - Refactor gate: allow runtime and artifact contract refactor before V2 paper mode
- `[x] AR6` After T24
  - Focus: production readiness for exit-aware stack, failure handling, operational playbooks
  - Refactor gate: allow packaging/deployment structure changes

## Completion Criteria (Phase 2)

Phase 2 is complete when:

1. T14-T24 are completed with passing tests.
2. AR4-AR6 reviews are completed.
3. Exit-policy assumptions and net-of-cost/slippage behavior are documented with reproducible artifacts.

## Current Active Task

- `[x] Phase 2 Complete` T14-T24 + AR4-AR6 closed

## Change Log

- 2026-02-22: Phase 2 execution plan initialized.
- 2026-02-22: T14 completed with strict exit-policy contract, parser/validator module, unit tests, and validation artifacts.
- 2026-02-22: T15 completed with path-aware CE/PE labels (TP/SL/time-stop/hold-extension fields), forward-path + no-lookahead tests, and `t15_label_path_report.json` artifact.
- 2026-02-22: T16 completed with intrabar-aware backtest mode (`path_v2`), explicit `exit_reason` outputs, deterministic tie-break tests, slippage accounting, and `t16_backtest_*` artifacts.
- 2026-02-22: AR4 completed; label/backtest contracts accepted and no blocking refactor required before T17 policy layer.
- 2026-02-22: T17 completed with dynamic exit-policy state module (trail/break-even/hold-extension), threshold-behavior tests, and `t17_dynamic_exit_policy_report.json` artifact.
- 2026-02-22: T18 completed with deterministic exit-policy optimization harness (tie-break/slippage/EOD grid), fold-consistency checks, reproducibility tests, and `t18_exit_policy_optimization_report.json` artifact.
- 2026-02-22: T19 completed with fixed-horizon vs path-v2 policy-profile comparison harness, consistency checks across profiles, deterministic tests, and `t19_strategy_comparison_v2_report.json` artifact.
- 2026-02-22: T20 completed with configurable fill/slippage models (constant, spread_fraction, liquidity_adjusted), stress/cost-accounting tests, and `t20_backtest_*` artifacts.
- 2026-02-22: T21 completed with replay evaluation harness (decision-to-outcome mapping), timestamp-alignment tests, and `t21_replay_evaluation_*` artifacts.
- 2026-02-22: AR5 completed; replay/backtest parity accepted and no blocking refactor required before T22 runtime changes.
- 2026-02-22: T22 completed with exit-aware replay mode (`replay-dry-run-v2`), position-state persistence events (ENTRY/MANAGE/EXIT/IDLE), and `t22_exit_aware_paper_events.jsonl` artifact.
- 2026-02-22: T23 completed with execution-quality drift monitoring (event mix, exit reasons, hold duration), synthetic-drift alert tests, and `t23_execution_monitoring_*` artifacts.
- 2026-02-22: T24 completed with V2 documentation pack (`operator_runbook_v2.md`, `model_card_v2_addendum.md`, `retraining_sop_v2_addendum.md`, `reproducibility_spec_v2.md`), clean-room Phase-2 reproducibility runner updates, and `t24_phase2_reproducibility_*` artifacts.
- 2026-02-22: AR6 completed; production-readiness review accepted with no blocking refactor.

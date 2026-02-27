# Hostile Review Report V1 (Architect + Trader Critic)

Date: `2026-02-22`  
Scope: Phase 1 + Phase 2 artifact-based challenge review.

## Executive Verdict

- Engineering quality: `PASS`
- Trading readiness (live capital): `FAIL (for now)`
- Paper/shadow readiness: `PASS`

## Findings (Severity Ordered)

### 1. Critical: Statistical depth is too thin for promotion

- Observation:
  - Core evaluation reports are based on one walk-forward fold (`fold_count=1`).
- Why this is critical:
  - one fold is not enough to claim robust edge across regimes.
- Evidence:
  - `ml_pipeline/artifacts/t07_walk_forward_report.json`
  - `ml_pipeline/artifacts/t10_strategy_comparison_report.json`
  - `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`
- Closure gate:
  - require multi-fold regime-segmented evaluation before live go/no-go.

### 2. Critical: Edge collapses under stronger execution realism

- Observation:
  - Phase-2 best profile shows positive net in comparison report.
  - liquidity-adjusted fill stress run turns net negative.
- Why this is critical:
  - live outcomes are execution-dominated; this can erase model alpha.
- Evidence:
  - Positive profile: `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`
  - Stress negative: `ml_pipeline/artifacts/t20_backtest_report.json`
- Closure gate:
  - pass threshold under realistic fill/latency assumptions, not idealized ones.

### 3. High: Directional imbalance suggests CE concentration risk

- Observation:
  - Best Phase-2 profile has `ce_trades=253` vs `pe_trades=2`.
  - Phase-1 default best mode is `ce_only`.
- Why this matters:
  - strategy may be regime-dependent and fragile when downside momentum dominates.
- Evidence:
  - `ml_pipeline/artifacts/t10_strategy_comparison_report.json`
  - `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`
- Closure gate:
  - regime-segmented and side-segmented stability metrics required.

### 4. High: Probability quality remains weak in validation slice

- Observation:
  - Validation ROC-AUC near random in baseline report (close to 0.5).
- Why this matters:
  - unstable probability quality makes threshold-driven execution brittle.
- Evidence:
  - `ml_pipeline/artifacts/t06_train_report.json`
- Closure gate:
  - calibration + reliability checks (planned in Phase 3 T26).

### 5. Medium: Replay evaluation can still be optimistic vs live routing

- Observation:
  - Replay evaluation shows high/clean matching under controlled conditions.
- Why this matters:
  - live broker behavior includes latency, rejects, partial fills, and idempotency races.
- Evidence:
  - `ml_pipeline/artifacts/t21_replay_evaluation_report.json`
  - `ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl`
- Closure gate:
  - broker adapter + reconciliation + partial-fill simulation (Phase 3 T28-T31).

## What Holds Strong Under Criticism

1. Clear architecture evolution with review gates AR1-AR6.
2. Good contract discipline and tests.
3. Reproducibility is proven in clean-room style:
   - `ml_pipeline/artifacts/t13_reproducibility_report.json`
   - `ml_pipeline/artifacts/t24_phase2_reproducibility_report.json`
4. Event contract validation now exists and passes on current event stream:
   - `ml_pipeline/artifacts/t25_decision_event_validation_report.json`

## Decision

- Approve continued development: `YES`
- Approve live capital deployment: `NO`
- Required before live:
  1. Multi-fold + regime-segmented evidence.
  2. Execution realism with broker-like fills/latency.
  3. Shadow reconciliation and automated guardrails.

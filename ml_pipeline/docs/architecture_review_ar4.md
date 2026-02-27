# Architecture Review AR4 (Post T16)

Date: `2026-02-22`  
Scope checkpoint: after T16 (Backtest Engine V2 intrabar exit simulation)

## Reviewed Areas

1. Label/Backtest contract alignment for path-aware exits
2. Intrabar sequencing determinism and tie-break handling
3. Cost/slippage accounting boundaries and backward compatibility

## Findings

1. T15 label outputs and T16 execution inputs are now aligned through explicit path columns:
   - `*_path_exit_reason`
   - `*_tp_price`, `*_sl_price`
   - `*_first_hit_offset_min`
2. Backtest supports dual execution modes:
   - `fixed_horizon` (legacy behavior)
   - `path_v2` (intrabar-aware exits)
3. Determinism is enforced for same-bar TP/SL collisions via explicit tie-break:
   - `--intrabar-tie-break sl|tp`
4. Net return accounting now supports:
   - `cost_per_trade`
   - `slippage_per_trade`
5. Existing strategy/training consumers remain stable because default mode remains `fixed_horizon`.

## Decisions

1. Keep `fixed_horizon` as default until T17 policy module and T18 optimization are ready.
2. Treat `path_v2` as the canonical execution semantics for exit-policy experiments.
3. Keep explicit `exit_reason` enum in trades output and report summary (`exit_reason_counts`).

## Refactor Actions

No blocking refactor required at AR4 gate.

Accepted follow-up actions:

1. Add trail/forced-EOD realized-price path integration in T17/T18 (currently optional/fallback).
2. Add richer timestamp realism tests for `forced_eod` transitions once execution policy state is introduced.

## Risks for T17+

1. Without policy-state tracking, trail exits rely on optional columns and fallback behavior.
2. Current path logic is deterministic but simplified; may understate microstructure uncertainty.
3. Exit-policy optimization search space can explode if constraints are not applied early.

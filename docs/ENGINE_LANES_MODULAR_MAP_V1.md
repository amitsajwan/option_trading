# Engine Lanes Modular Map v1

## Scope

Phase-1 modularization map for v2.3 lane-aware runtime, persistence, dashboard diagnostics, and promotion reporting.

## Lane Contract

- `engine_mode`: `deterministic|ml|ml_pure`
- `decision_mode`: `rule_vote|ml_gate|ml_dual`
- `decision_reason_code`: normalized reason code
- `decision_metrics`: optional numeric metrics
- `strategy_family_version`: `DET_V1|ML_GATE_V1|ML_PURE_DUAL_V1`
- `strategy_profile_id`: strategy set version id

## Owner Modules

- Decision contract normalization:
  - `contracts_app/strategy_decision_contract.py`
  - Inputs: raw reason/metric text + payload field candidates
  - Outputs: normalized engine/decision/reason + parsed numeric tokens

- Strategy logging:
  - `strategy_app/logging/signal_logger.py` (public facade)
  - `strategy_app/logging/decision_field_resolver.py`
  - `strategy_app/logging/jsonl_sink.py`
  - `strategy_app/logging/redis_event_publisher.py`
  - Inputs: `StrategyVote`, `TradeSignal`, `PositionContext`
  - Outputs: JSONL rows + Redis events with lane metadata

- Engine annotation:
  - `strategy_app/engines/decision_annotation.py`
  - Inputs: vote/signal + policy decisions + engine context
  - Outputs: populated lane contract fields on vote/signal objects

- Mongo persistence:
  - `persistence_app/mongo_writer.py`
  - Inputs: strategy events
  - Outputs: top-level queryable lane fields on `strategy_votes`, `trade_signals`, `strategy_positions`

- Live diagnostics and session assembly:
  - `market_data_dashboard/live_strategy_monitor_service.py` (public facade)
  - `market_data_dashboard/live_strategy_repository.py`
  - `market_data_dashboard/diagnostics/ml_gate.py`
  - `market_data_dashboard/diagnostics/ml_pure.py`
  - `market_data_dashboard/ux/decision_explainer.py`
  - `market_data_dashboard/ux/alerts.py`
  - `market_data_dashboard/live_strategy_session_assembler.py`
  - `market_data_dashboard/strategy_monitor_contracts.py`
  - Inputs: Mongo vote/signal/position docs
  - Outputs: `/api/live/strategy/session` payload with `engine_context`, `decision_diagnostics`, `promotion_lane`, `ops_state`, `active_alerts`, `decision_explainability`, `ui_hints`

- Promotion reporting:
  - `ml_pipeline/src/ml_pipeline/evaluation/futures_stage_metrics.py`
  - `ml_pipeline/src/ml_pipeline/evaluation/futures_promotion_ladder.py`
  - `ml_pipeline/src/ml_pipeline/evaluation/futures_direction_eval.py` (facade)
  - `ml_pipeline/src/ml_pipeline/publishing/promotion_summary.py`
  - Inputs: holdout eval report + training replay utility summary
  - Outputs: `promotion_ladders.ml_pure`, `promotion_ladders.deterministic`, `promotion_decision`

## Compatibility Notes

- Public APIs and CLI entrypoints remain unchanged.
- `ml_diagnostics` remains available as alias of `decision_diagnostics.ml_gate`.
- Legacy rows without new metadata continue to parse in monitoring APIs.
- Stage C remains non-blocking for model validity.

## Deprecation Timeline

- Temporary wrapper/compatibility paths are retained for one release cycle.
- Wrapper cleanup is allowed after parity tests and replay smoke checks stay green for one full cycle.

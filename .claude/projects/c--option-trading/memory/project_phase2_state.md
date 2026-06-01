---
name: project-phase2-state
description: Phase 2 stream-native pipeline consumers — implementation state as of 2026-05-31
metadata:
  type: project
---

Phase 2 (Sprint 4-5) is complete. 37 new consumer tests pass (all green).

## Files created

### Package
- `strategy_app/consumers/__init__.py` — exports all 6 consumers
- `strategy_app/consumers/_utils.py` — shared helpers: `parse_payload_from_fields`, `is_sentinel`, `atm_premium_for_direction`, `snapshot_trade_date`

### Stage consumers
| File | Stage | Reads | Writes |
|---|---|---|---|
| `regime_decision_consumer.py` | 1 | `snapshots` | `regime_decisions` |
| `entry_decision_consumer.py` | 2 | `regime_decisions` | `entry_decisions` |
| `direction_decision_consumer.py` | 3 | `entry_decisions` | `direction_decisions` |
| `strike_decision_consumer.py` | 4 | `direction_decisions` | `strike_decisions` |
| `risk_decision_consumer.py` | 5 | `strike_decisions` | `risk_decisions` |
| `execution_consumer.py` | 6 | `risk_decisions` | `execution_events` |

### Tests
- `strategy_app/tests/test_stage_consumers.py` — 37 tests, no Redis required

## Design notes
- Each consumer uses `StageBus.publish_decision()` which stamps run_id/parity_mode/plugin_id/plugin_version
- Each consumer handles sentinels gracefully (stops on sentinel message)
- Pending recovery: on startup, reads stream_id="0" first to re-deliver unacknowledged messages
- xack is always AFTER successful processing (failure leaves message in PEL for recovery)
- `RiskDecisionConsumer` maintains in-process `RiskManager` state — resets on restart (known limitation, to be fixed)
- `ExecutionConsumer` supports dual-publish to legacy pubsub topic via `EXECUTION_DUAL_PUBLISH=1` env flag

## Phase 2 DoD status
1. ✅ 7 streams defined: market_snapshots, regime_decisions, entry_decisions, direction_decisions, strike_decisions, risk_decisions, execution_events
2. ✅ Each engine is an independent consumer reading from one stream, writing to next
3. ✅ Every decision is traceable through trace_id (verified in test_trace_id_consistent_across_all_stages)
4. ✅ parent_event_id chain intact across all 6 stages (verified in test_parent_event_id_chain_is_intact)
5. ✅ No stage directly depends on another stage's implementation

## Known limitations to address in future sprints
- `RiskDecisionConsumer` state must be persisted to Redis/RuntimeArtifactStore for production safety
- `EntryDecisionConsumer` uses a synthetic StrategyVote — full StrategyRouter vote generation needed
- `DirectionDecisionConsumer` uses simplified direction signals — full ML bundle integration needed
- Stream names use `Namespace.stream_for()` — live/OOS mode uses pubsub topics instead

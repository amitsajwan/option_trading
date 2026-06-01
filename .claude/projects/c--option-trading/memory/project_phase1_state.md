---
name: project-phase1-state
description: Current implementation state of Phase 1-2 (event bus, contracts, stream topology) as of 2026-05-31
metadata:
  type: project
---

Sprint 1-3 of Phase 1 is complete. All 66 new tests pass.

## Files created (new)
- `contracts_app/event_bus.py` — `EventBus` ABC + `RedisEventBus` (publish/consume/acknowledge/ensure_group/ping). `consume()` supports `stream_id=">"` (new) or `stream_id="0"` (pending recovery).
- `contracts_app/parity_mode.py` — `ParityMode(str, Enum)` with LIVE_FULL/REPLAY_SNAPSHOT_ONLY/REPLAY_FULL + `infer_parity_mode(source_mode)`.
- `contracts_app/decision_events.py` — 6 typed `@dataclass` event types (`RegimeDecisionEvent`, `EntryDecisionEvent`, `DirectionDecisionEvent`, `StrikeDecisionEvent`, `RiskDecisionEvent`, `ExecutionEvent`), all extending `BaseDecisionEvent` (8 required metadata fields). build_*/parse_* factories.
- `strategy_app/runtime/stage_bus.py` — `StageBus` + `StageBusConfig` context wrapper; stamps run_id/parity_mode/plugin_id/plugin_version onto every `publish_decision()` call.
- `strategy_app/market/regime_plugin_adapter.py` — `RegimeClassifierAdapter(RegimePlugin)` wrapping existing `RegimeClassifier`.
- `strategy_app/tests/test_event_envelope_contract.py` — 46 tests covering all 6 event types, ParityMode, StageBus. No Redis required.
- `strategy_app/tests/test_regime_plugin_contract.py` — 20 tests covering RegimePlugin ABC, RegimeDecisionResult, RegimeClassifierAdapter behaviour.

## Files modified
- `contracts_app/events.py` — added nullable `trace_id`, `parent_event_id` to `SnapshotEventEnvelope` and all 4 build_*_event() helpers (additive, backward-compat).
- `contracts_app/topics.py` — added 6 new topic functions: `regime_decisions_topic()`, `entry_decisions_topic()`, `direction_decisions_topic()`, `strike_decisions_topic()`, `risk_decisions_topic()`, `execution_events_topic()`.
- `contracts_app/sim_namespace.py` — added 6 new slugs to `_NAMESPACED_BASES`: regime_decisions, entry_decisions, direction_decisions, strike_decisions, risk_decisions, execution_events.
- `contracts_app/__init__.py` — exports all new classes/functions.
- `strategy_app/brain/plugin.py` — added `RegimeDecisionResult` NamedTuple + `RegimePlugin` ABC.
- `strategy_app/logging/redis_event_publisher.py` — constructor now accepts `bus: Optional[EventBus]`; falls back to `RedisEventBus()` if not provided. No direct `redis.Redis` import.
- `strategy_app/health.py` — Redis ping extracted to `_check_redis(bus=None)` helper; removed top-level `import redis`.
- `strategy_app/runtime/redis_snapshot_consumer.py` — added `bus: Optional[EventBus]` param; `_ensure_stream_group`, `_read_stream_batch`, and xack calls now delegate to bus when provided. Added `_ack()` helper that ensures xack is ALWAYS post-evaluate.
- `strategy_app/runtime/redis_depth_reader.py` — removed top-level `import redis`; `_redis_client()` now lazy-imports. Constructor accepts injected `client=`.

## Phase 1 DoD status
1. ✅ EventBus class with publish/consume/acknowledge
2. ✅ 7 stream names registered (sim_namespace + topics)
3. ✅ All 6 required canonical event types defined
4. ✅ All 8 metadata fields on every event
5. ✅ RegimePlugin ABC + RegimeClassifierAdapter
6. ✅ Business logic no longer calls redis.Redis directly (health.py has controlled fallback)
7. ⬜ Consumers not yet split into independent processes (Phase 2, Sprint 4-5)

## Next: Sprint 4 (Phase 2)
Create `strategy_app/consumers/` package with 6 independent stage consumers:
- `regime_decision_consumer.py`
- `entry_decision_consumer.py`
- `direction_decision_consumer.py`
- `strike_decision_consumer.py`
- `risk_decision_consumer.py`
- `execution_consumer.py`

**Why:** Phase 2 DoD requires each engine be an independent stream consumer.
**How to apply:** When building consumers, check existing engine classes to extract business logic: `EntryPolicy` in `policy/entry_policy.py`, `direction_consensus.py`, `option_selector.py`, `risk/manager.py`.

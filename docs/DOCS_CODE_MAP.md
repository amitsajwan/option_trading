# Docs to Code Map

This file maps active docs to the code paths they describe.

## 1. Active Docs

| Doc | Authoritative code paths | Key entrypoints |
|---|---|---|
| `docs/SYSTEM_SOURCE_OF_TRUTH.md` | `strategy_app/main.py`, `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`, `snapshot_app/historical/window_manifest.py` | `python -m strategy_app.main ...`, `python -m ml_pipeline_2.run_research ...` |
| `docs/STRATEGY_SYSTEM_VALIDATION_2026-03-19.md` | `strategy_app/engines/deterministic_rule_engine.py`, `strategy_app/engines/strategy_router.py`, `strategy_app/engines/strategies/all_strategies.py`, `strategy_app/runtime/redis_snapshot_consumer.py`, `strategy_app/risk/manager.py`, `ml_pipeline_2/src/ml_pipeline_2/labeling/engine.py`, `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py` | `python -m pytest strategy_app/tests/test_position_risk.py ...`, `python -m pytest ml_pipeline_2/tests/test_staged_pipeline.py ...` |
| `docs/SUPPORT_BRINGUP_GUIDE.md` | `docker-compose.yml`, `strategy_app/main.py`, `strategy_app/runtime/redis_snapshot_consumer.py` | `docker compose ...`, `python -m strategy_app.health` |
| `docs/ARCHITECTURE.md` | `snapshot_app/core/market_snapshot.py`, `strategy_app/runtime/redis_snapshot_consumer.py`, `persistence_app/main_snapshot_consumer.py` | `python -m snapshot_app.main_live ...`, `python -m strategy_app.main ...` |
| `docs/strategy_catalog.md` | `strategy_app/engines/strategies/all_strategies.py`, `strategy_app/engines/strategy_router.py`, `strategy_app/engines/deterministic_rule_engine.py` | `python -m strategy_app.main --engine deterministic ...` |
| `strategy_app/docs/STRATEGY_ML_FLOW.md` | `strategy_app/engines/deterministic_rule_engine.py`, `strategy_app/engines/pure_ml_engine.py`, `strategy_app/risk/manager.py`, `strategy_app/runtime/redis_snapshot_consumer.py` | `python -m strategy_app.main --engine deterministic ...`, `python -m strategy_app.main --engine ml_pure ...` |
| `strategy_app/docs/detailed-design.md` | `strategy_app/main.py`, `strategy_app/engines/*`, `strategy_app/logging/*`, `strategy_app/position/*`, `strategy_app/risk/*` | `python -m strategy_app.main ...` |

## 2. Package Ownership

| Package | Canonical docs |
|---|---|
| `strategy_app` | `strategy_app/docs/README.md`, `strategy_app/docs/detailed-design.md`, `strategy_app/docs/STRATEGY_ML_FLOW.md` |
| `ml_pipeline_2` | `ml_pipeline_2/docs/architecture.md`, `ml_pipeline_2/docs/detailed_design.md`, `ml_pipeline_2/docs/gcp_user_guide.md` |
| `snapshot_app` | `docs/ARCHITECTURE.md`, `docs/SYSTEM_SOURCE_OF_TRUTH.md` |
| `persistence_app` | `docs/ARCHITECTURE.md`, `docs/PROCESS_TOPOLOGY.md` |

## 3. Notes

- Legacy runtime overlay docs were retired during engine consolidation.
- The validation doc above is the current cross-module correction layer when older strategy docs drift from code.
- Historical archive material remains under `docs/archive/` only.
- When code paths move, update this map in the same change.

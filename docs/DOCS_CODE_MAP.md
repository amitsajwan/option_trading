# Docs to Code Map

This file is the canonical map between active docs, code ownership, and operational artifacts.

## 1. Active Doc -> Authoritative Code Paths -> Entry Commands

| Doc | Authoritative code paths | Key entrypoints / commands |
|---|---|---|
| `docs/SYSTEM_SOURCE_OF_TRUTH.md` | `snapshot_app/historical/window_manifest.py`, `strategy_app/tools/open_search_rebaseline_cycle.py`, `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`, `strategy_app/main.py` | `python -m snapshot_app.historical.snapshot_batch_runner --validate-only ...`, `python -m strategy_app.tools.open_search_rebaseline_cycle ...`, `python -m strategy_app.main ...` |
| `docs/ARCHITECTURE.md` | `snapshot_app/market_snapshot.py`, `contracts_app/events.py`, `contracts_app/topics.py`, `strategy_app/runtime/redis_snapshot_consumer.py`, `persistence_app/main_snapshot_consumer.py` | `python -m snapshot_app.main_live ...`, `python -m strategy_app.main ...`, `python -m persistence_app.main_snapshot_consumer` |
| `docs/PROCESS_TOPOLOGY.md` | `docker-compose.yml`, `start_apps.py`, `stop_apps.py` | `docker compose --env-file .env.compose up ...`, `python -m start_apps`, `python -m stop_apps` |
| `docs/SUPPORT_BRINGUP_GUIDE.md` | `ingestion_app/kite_auth.py`, `strategy_app/main.py`, `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`, `snapshot_app/health.py`, `strategy_app/health.py` | `python -m ingestion_app.kite_auth --verify`, `docker compose logs ...`, `python -m snapshot_app.health ...`, `python -m strategy_app.health` |
| `docs/strategy_catalog.md` | `strategy_app/engines/strategies/all_strategies.py`, `strategy_app/engines/strategy_router.py`, `strategy_app/engines/deterministic_rule_engine.py`, `strategy_app/engines/regime.py` | `python -m strategy_app.main --engine deterministic ...` |
| `docs/strategy_eval_architecture.md` | `market_data_dashboard/strategy_evaluation_service.py`, `strategy_eval_orchestrator/main.py`, `strategy_app/tools/deterministic_open_matrix.py`, `ml_pipeline/src/ml_pipeline/entry_quality_replay_eval.py` | `python -m strategy_eval_orchestrator.main`, `python -m strategy_app.tools.deterministic_open_matrix ...`, `python -m ml_pipeline.entry_quality_replay_eval ...` |
| `docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md` | `strategy_app/tools/open_search_rebaseline_cycle.py`, `snapshot_app/historical/snapshot_batch_runner.py`, `ml_pipeline/src/ml_pipeline/entry_candidate_dataset.py`, `ml_pipeline/src/ml_pipeline/entry_quality_experiments.py` | `python -m strategy_app.tools.open_search_rebaseline_cycle ...` |

## 2. Package -> Canonical Doc

| Package | Canonical doc |
|---|---|
| `snapshot_app` | `docs/ARCHITECTURE.md`, `docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md` |
| `strategy_app` | `docs/strategy_catalog.md`, `docs/SYSTEM_SOURCE_OF_TRUTH.md`, `docs/SUPPORT_BRINGUP_GUIDE.md` |
| `ml_pipeline` | `docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md`, `docs/strategy_eval_architecture.md` |
| `persistence_app` | `docs/ARCHITECTURE.md`, `docs/PROCESS_TOPOLOGY.md` |
| `strategy_eval_orchestrator` | `docs/strategy_eval_architecture.md`, `docs/PROCESS_TOPOLOGY.md` |
| `market_data_dashboard` | `docs/PROCESS_TOPOLOGY.md`, `docs/SUPPORT_BRINGUP_GUIDE.md` |
| `contracts_app` | `docs/ARCHITECTURE.md` |

## 3. Runtime Artifacts/Logs -> Owning Process -> Doc

| Artifact / log | Owner | Doc |
|---|---|---|
| `.run/snapshot_app/events.jsonl` | `snapshot_app.main_live` | `docs/PROCESS_TOPOLOGY.md`, `docs/SUPPORT_BRINGUP_GUIDE.md` |
| `.run/strategy_app/votes.jsonl` | `strategy_app.main` | `docs/PROCESS_TOPOLOGY.md`, `docs/SUPPORT_BRINGUP_GUIDE.md` |
| `.run/strategy_app/signals.jsonl` | `strategy_app.main` | `docs/PROCESS_TOPOLOGY.md`, `docs/SUPPORT_BRINGUP_GUIDE.md` |
| `.run/strategy_app/positions.jsonl` | `strategy_app.main` | `docs/PROCESS_TOPOLOGY.md`, `docs/SUPPORT_BRINGUP_GUIDE.md` |
| `.run/open_search_rebaseline*/<cycle_id>/cycle_summary.json` | `strategy_app.tools.open_search_rebaseline_cycle` | `docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md`, `docs/strategy_eval_architecture.md` |
| `.run/window_manifest*.json` | `snapshot_app.historical.snapshot_batch_runner` | `docs/SYSTEM_SOURCE_OF_TRUTH.md`, `docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md` |

## 4. Notes

- Archived docs under `docs/archive/` are historical references only and are not part of active operating contracts.
- When updating architecture or runbooks, update this mapping in the same PR.
- For the first supported fresh-machine milestone, `Live+Dashboard` is the active runtime target. `strategy_eval_architecture` and `OPEN_SEARCH_REBASELINE_RUNBOOK` remain legacy/offline references and are not part of the supported live runtime path.

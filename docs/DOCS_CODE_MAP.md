# Docs to Code Map

This file maps the active docs to the code they describe.

Root `docs/` is reserved for cross-cutting system material.
Operator procedures live under `docs/runbooks/`.
Package-specific docs live under the owning package.

## 1. Cross-Cutting Docs

| Doc | Authoritative code paths | Key entrypoints |
|---|---|---|
| `docs/SYSTEM_SOURCE_OF_TRUTH.md` | `strategy_app/main.py`, `ml_pipeline_2/src/ml_pipeline_2/run_staged_release.py`, `ml_pipeline_2/src/ml_pipeline_2/publishing/resolver.py`, `ops/gcp/publish_runtime_config.sh` | `python -m strategy_app.main ...`, `python -m ml_pipeline_2.run_staged_release ...` |
| `docs/ARCHITECTURE.md` | `snapshot_app/core/market_snapshot.py`, `strategy_app/main.py`, `strategy_app/engines/pure_ml_engine.py`, `ml_pipeline_2/src/ml_pipeline_2/staged/publish.py` | `python -m snapshot_app.main_live ...`, `python -m strategy_app.main ...`, `python -m ml_pipeline_2.run_staged_release ...` |
| `docs/PROCESS_TOPOLOGY.md` | `docker-compose.yml`, `start_apps.py`, `stop_apps.py`, `strategy_app/main.py` | `docker compose ...`, `python -m start_apps ...`, `python -m stop_apps ...` |

## 2. Runbooks

| Doc | Authoritative code paths | Key entrypoints |
|---|---|---|
| `docs/runbooks/README.md` | `ops/gcp/README.md`, `ops/gcp/create_training_vm.sh`, `ops/gcp/run_snapshot_parquet_pipeline.sh`, `ops/gcp/run_staged_release_pipeline.sh` | `./ops/gcp/...` |
| `docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md` | `snapshot_app/historical/snapshot_batch_runner.py`, `ops/gcp/run_snapshot_parquet_pipeline.sh`, `ops/gcp/publish_snapshot_parquet.sh` | `python -m snapshot_app.historical.snapshot_batch_runner ...`, `./ops/gcp/run_snapshot_parquet_pipeline.sh` |
| `docs/runbooks/TRAINING_RELEASE_RUNBOOK.md` | `ops/gcp/from_scratch_bootstrap.sh`, `ops/gcp/create_training_vm.sh`, `ops/gcp/run_staged_release_pipeline.sh`, `ml_pipeline_2/src/ml_pipeline_2/run_staged_release.py` | `./ops/gcp/create_training_vm.sh`, `./ops/gcp/run_staged_release_pipeline.sh` |
| `docs/runbooks/GCP_DEPLOYMENT.md` | `ops/gcp/from_scratch_bootstrap.sh`, `ops/gcp/build_runtime_images.sh`, `ops/gcp/publish_runtime_config.sh`, `ops/gcp/apply_ml_pure_release.sh`, `infra/gcp/templates/runtime-startup.sh.tftpl` | `./ops/gcp/build_runtime_images.sh`, `./ops/gcp/publish_runtime_config.sh` |
| `docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md` | `ops/gcp/delete_training_vm.sh`, `ops/gcp/stop_runtime.sh`, `ops/gcp/destroy_infra_preserve_data.sh` | `./ops/gcp/...` |

## 3. Package Docs

| Package | Canonical docs |
|---|---|
| `strategy_app` | `strategy_app/docs/README.md`, `strategy_app/docs/CURRENT_TREE_VALIDATION.md`, `strategy_app/docs/strategy_catalog.md`, `strategy_app/docs/detailed-design.md`, `strategy_app/docs/STRATEGY_ML_FLOW.md` |
| `ml_pipeline_2` | `ml_pipeline_2/docs/README.md`, `ml_pipeline_2/docs/architecture.md`, `ml_pipeline_2/docs/detailed_design.md`, `ml_pipeline_2/docs/gcp_user_guide.md` |
| `snapshot_app` | `snapshot_app/historical/README.md`, `docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md` |
| `persistence_app` | `docs/ARCHITECTURE.md`, `docs/PROCESS_TOPOLOGY.md` |

## 4. Notes

- three primary operator workflows are:
  - historical snapshot creation
  - staged training release
  - live runtime deployment
- supported live runtime lane: `ml_pure`
- deterministic is the replay and research lane
- supported ML training and publish lane: staged `ml_pipeline_2`
- retired open-search, champion-registry, fixed-strike remediation, and archive docs were removed from the active tree
- when code paths or canonical doc locations move, update this map in the same change

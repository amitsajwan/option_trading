# BankNifty Options Algo - Repo Guide

This repo is a microservice-based trading data platform with live and historical snapshot pipelines.

If you are new, follow this reading order:

1. [docs/SYSTEM_SOURCE_OF_TRUTH.md](docs/SYSTEM_SOURCE_OF_TRUTH.md)
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
3. [docs/SUPPORT_BRINGUP_GUIDE.md](docs/SUPPORT_BRINGUP_GUIDE.md)
4. [docs/PROCESS_TOPOLOGY.md](docs/PROCESS_TOPOLOGY.md)
5. [docs/strategy_eval_architecture.md](docs/strategy_eval_architecture.md)
6. [docs/strategy_catalog.md](docs/strategy_catalog.md)
7. [docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md](docs/OPEN_SEARCH_REBASELINE_RUNBOOK.md)
8. [docs/DOCS_CODE_MAP.md](docs/DOCS_CODE_MAP.md)
9. Service-level READMEs (linked below)
10. [docs/FROM_SCRATCH_OPERATOR_GUIDE.md](docs/FROM_SCRATCH_OPERATOR_GUIDE.md)
11. [docs/GCP_DEPLOYMENT.md](docs/GCP_DEPLOYMENT.md)
12. [docs/GCP_FRESH_START.md](docs/GCP_FRESH_START.md)

ML note:

- supported ML training, threshold sweep, publishing, and `ml_pure` runtime switching now live in `ml_pipeline_2`
- preferred operator flow is now the guarded `ml_pipeline_2.run_recovery_release` command for train/sweep/publish/GCS handoff
- deprecated `ml_pipeline` may still exist for legacy historical/eval tooling, but it is not part of the supported Live+Dashboard runtime path
- `strategy_app` remains the live runtime consumer for `deterministic` and `ml_pure`

## Services

- `ingestion_app`: market data API + session-aware live runner
- `snapshot_app`: builds and publishes canonical MarketSnapshot (MSS.1-MSS.9)
- `persistence_app`: consumes snapshot and strategy events and writes to MongoDB
- `strategy_app`: consumes snapshots for deterministic/ML strategy logic
- `market_data_dashboard` (optional): UI + monitoring APIs
- `contracts_app`: shared contracts (topics/events/session/math)

## Runtime Profiles

- Baseline live stack: `redis`, `mongo`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_app`, `strategy_persistence_app`
- Optional dashboard profile: `dashboard`
- Legacy historical strategy profile: `persistence_app_historical`, `strategy_app_historical`, `strategy_persistence_app_historical`
- Legacy manual historical replay profile: `historical_replay`

Supported E2E target for this milestone:

- `Live+Dashboard` only
- `strategy_app` engine support:
  - `deterministic`
  - `ml_pure`
- no supported live/runtime service should require `ml_pipeline` on `PYTHONPATH`

## Quick Start (Docker Compose)

```bash
cp .env.compose.example .env.compose
docker compose --env-file .env.compose up -d --build redis mongo ingestion_app snapshot_app persistence_app strategy_app strategy_persistence_app
```

To enable registry-backed ML entry gating in `strategy_app`, set these in `.env.compose`:

```bash
STRATEGY_ML_ENTRY_REGISTRY=.run/canonical_eq_e2e_refreshed_rerun2/eval/evaluation_registry.csv
STRATEGY_ML_ENTRY_EXPERIMENT_ID=eq_core_snapshot_v1__mfe15_gt_5_v1__seg_regime_v1__lgbm_default_v1__fixed_060
```

Compose injects those values into the container as `ML_ENTRY_REGISTRY` and `ML_ENTRY_EXPERIMENT_ID`.
Direct CLI flags still override env values for local/manual runs.

To enable `ml_pure` runtime from published `ml_pipeline_2` artifacts, set:

```bash
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=<published_run_id>
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
```

Optional explicit-path mode:

```bash
ML_PURE_MODEL_PACKAGE=<abs-or-repo-relative-model.joblib>
ML_PURE_THRESHOLD_REPORT=<abs-or-repo-relative-threshold_report.json>
```

Do not mix run-id mode and explicit-path mode in one launch.

If PowerShell interpolation looks wrong, clear stale shell vars before `docker compose`:

```powershell
Remove-Item Env:ML_ENTRY_REGISTRY -ErrorAction SilentlyContinue
Remove-Item Env:ML_ENTRY_EXPERIMENT_ID -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_REGISTRY -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_EXPERIMENT_ID -ErrorAction SilentlyContinue
```

Optional dashboard:

```bash
docker compose --env-file .env.compose --profile ui up -d dashboard
```

## GCP Deployment

For repeatable GCP deployment, use:

- [docs/GCP_DEPLOYMENT.md](docs/GCP_DEPLOYMENT.md) for the runtime/training architecture
- [infra/gcp/README.md](infra/gcp/README.md) for Terraform scaffolding
- [docker-compose.gcp.yml](docker-compose.gcp.yml) for Artifact Registry-backed service images

Recommended production shape:

- small always-on runtime VM for Live+Dashboard
- separate disposable high-memory training VM
- Artifact Registry for application images
- Cloud Storage for published models and frozen ML inputs

The optional dashboard profile is part of the supported target.
Historical replay and eval profiles are still legacy and not part of the first supported fresh-machine E2E path.

Optional historical replay:

```bash
docker compose --env-file .env.compose --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical
```

Manual replay job (one-shot):

```bash
docker compose --env-file .env.compose --profile historical_replay up historical_replay
```

This historical profile uses isolated topics and Mongo collections so replay does not mix with live strategy telemetry.

Mongo date-select replay (host command):

```powershell
python -m snapshot_app.historical.mongo_replay_runner --date 2026-03-06 --mongo-port 27019
```

Evaluate persisted historical strategy results:

```bash
docker compose --profile historical exec -T strategy_persistence_app_historical python -m persistence_app.strategy_evaluation --date-from 2024-01-01 --date-to 2024-01-31 --limit 20
```

Save the evaluation JSON on the host:

```bash
docker compose --profile historical exec -T strategy_persistence_app_historical python -m persistence_app.strategy_evaluation --date-from 2024-01-01 --date-to 2024-01-31 --limit 20 > .run/strategy_evaluation_2024-01.json
```

## Local Start (No Compose)

Use one local launcher path:

```bash
python -m start_apps --include-dashboard
```

Stop:

```bash
python -m stop_apps --include-dashboard
```

## Key Runtime Rules

- Session-gated live processing (IST market hours).
- Fail-closed token behavior (invalid/missing token keeps ingestion idle).
- Live and historical topics are isolated.
- Snapshot builder contract is centralized in `snapshot_app.market_snapshot`.

## Service Docs

- [ingestion_app/README.md](ingestion_app/README.md)
- [snapshot_app/README.md](snapshot_app/README.md)
- [persistence_app/README.md](persistence_app/README.md)
- [strategy_app/README.md](strategy_app/README.md)
- [market_data_dashboard/README.md](market_data_dashboard/README.md)

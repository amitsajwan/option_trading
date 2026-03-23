# BankNifty Options Algo - Repo Guide

This repo is a cross-service trading runtime plus staged ML training pipeline.

Root `docs/` contains cross-cutting system docs.
Operator runbooks live under `docs/runbooks/`.
Package-specific design and runtime notes live under the owning package:

- `strategy_app/docs`
- `ml_pipeline_2/docs`
- `snapshot_app/historical`

If you are new, read in this order:

1. [docs/SYSTEM_SOURCE_OF_TRUTH.md](docs/SYSTEM_SOURCE_OF_TRUTH.md)
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
3. [docs/runbooks/README.md](docs/runbooks/README.md)
4. [docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
5. [docs/runbooks/TRAINING_RELEASE_RUNBOOK.md](docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
6. [docs/runbooks/GCP_DEPLOYMENT.md](docs/runbooks/GCP_DEPLOYMENT.md)
7. [docs/PROCESS_TOPOLOGY.md](docs/PROCESS_TOPOLOGY.md)
8. [strategy_app/docs/README.md](strategy_app/docs/README.md)
9. [ml_pipeline_2/docs/README.md](ml_pipeline_2/docs/README.md)
10. [snapshot_app/historical/README.md](snapshot_app/historical/README.md)
11. [docs/DOCS_CODE_MAP.md](docs/DOCS_CODE_MAP.md)

Current runtime and training rules:

- supported live runtime lane: `strategy_app --engine ml_pure`
- deterministic runtime: replay and research only
- supported ML training and publish lane: staged `ml_pipeline_2`
- retired `ml_pipeline`, open-search, and champion-registry flows are not part of the current branch

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
- Historical replay profile: `persistence_app_historical`, `strategy_app_historical`, `strategy_persistence_app_historical`
- Manual historical replay profile: `historical_replay`

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

Compose startup is expected to work from repo root.

The two external prerequisites that commonly block a fully live stack are:

1. `ingestion_app/credentials.json` for live Kite/NSE access
2. a valid staged `ml_pure` handoff plus runtime guard if `STRATEGY_ENGINE=ml_pure`

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
Remove-Item Env:ML_RUNTIME_GUARD_FILE -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_RUN_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_GROUP -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_PACKAGE -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_THRESHOLD_REPORT -ErrorAction SilentlyContinue
```

Optional dashboard:

```bash
docker compose --env-file .env.compose --profile ui up -d dashboard
```

## Local Python For VS Code

The runtime is container-first, but VS Code still needs a local Python interpreter if you want:

- Python language service and imports to resolve locally
- host-side `pytest` runs
- host-side helper commands like `python -m start_apps`

On this Windows workspace, VS Code will not recognize Python until Python itself is installed on the host.

Recommended Windows bootstrap:

```powershell
winget install -e --id Python.Python.3.11
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r market_data_dashboard\requirements.txt
python -m pip install -r persistence_app\requirements.txt
python -m pip install -r snapshot_app\requirements.txt
python -m pip install -r strategy_app\requirements.txt
```

This repo now includes `.vscode/settings.json` with:

- interpreter path: `.venv\\Scripts\\python.exe`
- pytest discovery enabled

After creating `.venv`, run `Python: Select Interpreter` in VS Code once if it does not pick it up automatically.

## GCP Deployment

For repeatable GCP operations, use:

- [docs/runbooks/README.md](docs/runbooks/README.md) for the workflow index
- [docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) for historical parquet creation
- [docs/runbooks/TRAINING_RELEASE_RUNBOOK.md](docs/runbooks/TRAINING_RELEASE_RUNBOOK.md) for staged training and publish
- [docs/runbooks/GCP_DEPLOYMENT.md](docs/runbooks/GCP_DEPLOYMENT.md) for runtime deploy/cutover
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
- Snapshot builder contract is centralized in `snapshot_app.core.market_snapshot`.

## Service Docs

- [ingestion_app/README.md](ingestion_app/README.md)
- [snapshot_app/README.md](snapshot_app/README.md)
- [persistence_app/README.md](persistence_app/README.md)
- [strategy_app/README.md](strategy_app/README.md)
- [market_data_dashboard/README.md](market_data_dashboard/README.md)

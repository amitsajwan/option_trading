# AGENTS.md

## Cursor Cloud specific instructions

### Overview

BankNifty Options Algo — monorepo with Python backend services + a React/Vite frontend (`strategy_eval_ui`). All Python services share `contracts_app` as a local package. The ML pipeline (`ml_pipeline_2`) is also an editable package.

### Running services locally (without Docker app containers)

Redis and MongoDB are required infrastructure. Start them via Docker Compose:

```bash
docker compose up -d redis mongo
```

The dashboard can be run directly (no Docker build needed):

```bash
source .venv/bin/activate
export REDIS_HOST=localhost REDIS_PORT=6379 REDIS_DB=0
export MONGO_HOST=localhost MONGO_PORT=27017 MONGO_DB=trading_ai
export DASHBOARD_PORT=8008 MARKET_DATA_API_URL=http://127.0.0.1:8004
export INSTRUMENT_SYMBOL=BANKNIFTY26MARFUT
export DASHBOARD_CORS_ORIGINS="http://localhost:8011,http://127.0.0.1:8011,http://localhost:5173,http://127.0.0.1:5173"
python -m uvicorn market_data_dashboard.app:app --host 0.0.0.0 --port 8008
```

Dashboard health: `curl http://localhost:8008/api/health`

### Tests

```bash
source .venv/bin/activate
python -m pytest strategy_app/tests/ -q          # strategy engine tests
python -m pytest market_data_dashboard/ -q        # dashboard tests
python -m pytest tests/ -q                        # integration/boundary tests
```

Frontend type-check: `cd strategy_eval_ui && npx tsc -b --noEmit`

### Known pre-existing test failures

- 14 failures in `strategy_app/tests/` due to `TradeSignal.lots` attribute being removed but tests not updated.
- 1 failure in `tests/test_live_runtime_boundaries.py` — asserts `deterministic` as historical default, but compose now uses `ml_pure`.

### GCP deploy (single VM preferred)

**Target:** one VM for **runtime + ML** — see [docs/GCP_UNIFIED_VM.md](docs/GCP_UNIFIED_VM.md).

| Item | Value |
|------|--------|
| Project | `algo-trading-496203` |
| Zone | `asia-south1-b` |
| Recommended type | **`e2-highmem-16`** (16 vCPU, 128 GB RAM) |
| Checkout | `/opt/option_trading` |
| Legacy VMs | `option-trading-runtime-01`, `option-trading-ml-01` (merge then stop ML VM) |

**Workflow:** commit → `git push` → VM `git pull` → `docker compose build` + `up -d --force-recreate --pull never` for runtime changes (`docker-compose.gcp.yml` otherwise pulls stale GHCR and ignores the local build).

- **Do not** `gcloud compute scp` application source.
- **ML jobs** (HPO, parquet): same VM, prefer **off-market hours**; oracle labeling needs **≥64 GB** RAM.
- **`n2-highmem-*`** may be unavailable in zone; **`e2-highmem-*`** has worked.
- **Skill locations:**
  - `.cursor/rules/gcp-deploy-workflow.mdc` — Cursor rule for GCP deploy
  - `.cursor/skills/gcp-vm-deploy/SKILL.md` — Comprehensive GCP deploy skill
  - `.windsurf/skills/gcp/SKILL.md` — Windsurf GCP deploy skill (upgraded ✓)
  - `.cursor/workflows/gcp-deploy.md` — GCP deploy workflow steps
  - `.windsurf/workflows/gcp-deploy.md` — GCP deploy workflow steps
  - `.cursor/workflows/run-tests.md` — Test execution workflow
  - `.windsurf/workflows/run-tests.md` — Test execution workflow
- **Exit/risk experiments (May–Jul OOS):** `docs/EXIT_RISK_EXPERIMENTS_2026-05.md` — E1–E5 replay grid, scorecard, consensus profile.

### Gotchas

- Docker daemon must be started manually (`sudo dockerd &`) since this runs inside a container. Socket permissions need `sudo chmod 666 /var/run/docker.sock` after daemon start.
- `ingestion_app` requires Zerodha Kite API credentials (`credentials.json`) — it will not start without them. The dashboard shows `market_data_api: unhealthy` when ingestion is unavailable; this is expected in dev without Kite credentials.
- The venv must be activated from `/workspace` so that all service packages resolve correctly (they're imported as top-level packages, not installed separately).
- `contracts_app` must be installed in editable mode (`pip install -e ./contracts_app`) before any service imports work.
- `ml_pipeline_2` is also editable (`pip install -e ./ml_pipeline_2`) and is pulled in transitively by `requirements-test.txt`.
- The frontend (`strategy_eval_ui`) uses npm with no lockfile committed. Install with `npm install` and build with `npm run build`.

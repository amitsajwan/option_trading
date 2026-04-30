# BankNifty Options Algo — Operations Guide

**This is the starting point.** Read this first. Everything else is linked from here.

As-of: `2026-04-27`

---

## What This System Is

An end-to-end algorithmic trading runtime for BankNifty options on NSE. It ingests live market data, builds canonical snapshots, runs a 3-stage ML strategy, persists all events to MongoDB, and serves a monitoring dashboard.

The ML pipeline (`ml_pipeline_2`) trains offline and publishes model bundles to GCS. The live runtime loads them at startup.

---

## Live State on GCP (right now)

**VM:** `option-trading-runtime-01` · `asia-south1-b` · project `amittrading-493606`

```bash
gcloud compute ssh savitasajwan03@option-trading-runtime-01 --zone asia-south1-b --project amittrading-493606
```

| Container | Status | Port |
|---|---|---|
| `strategy_app` | healthy | — |
| `dashboard` | healthy | 8008 |
| `ingestion_app` | healthy | 8004 |
| `snapshot_app` | **unhealthy** — check logs | — |
| `persistence_app` | healthy | — |
| `strategy_persistence_app` | healthy | — |
| `strategy_app_historical` | healthy | — |
| `strategy_persistence_app_historical` | healthy | — |
| `strategy_eval_orchestrator` | running | — |
| `redis` | healthy | 6379 |
| `mongo` | healthy | 27017 |

**Current engine on live VM:** `deterministic` (ML paths not yet applied — see [Model Switch](#model-switching) below)

**Pending change:** `.env.compose` updated locally to `ml_pure` + `staged_simple_s2_v1` (research/paper). Deploy when ready — see [Deploy](#3-deploy-to-gcp).

**Dashboard:** `http://<vm-external-ip>:8008`

---

## Services

| Service | What it does | Dockerfile |
|---|---|---|
| `ingestion_app` | Live market data API, Kite session, live snapshot runner | `ingestion_app/Dockerfile` |
| `snapshot_app` | Builds canonical MarketSnapshot (MSS.1–MSS.9), publishes to Redis | `snapshot_app/Dockerfile` |
| `persistence_app` | Writes snapshots + strategy events to MongoDB | `persistence_app/Dockerfile` |
| `strategy_app` | Strategy engine (deterministic or ml_pure), emits TradeSignals | `strategy_app/Dockerfile` |
| `strategy_persistence_app` | Writes strategy votes/signals/positions to MongoDB | `persistence_app/Dockerfile` |
| `market_data_dashboard` | UI + monitoring APIs + model catalog | `market_data_dashboard/Dockerfile` |
| `strategy_eval_orchestrator` | Orchestrates historical evaluation runs | `strategy_eval_orchestrator/` |
| `redis` | Event bus | upstream image |
| `mongo` | Persistence store | upstream image |

Historical replay uses isolated variants (`*_historical`) that write to separate MongoDB collections and Redis topics — they never touch live data.

---

## Daily Workflow

### 1. Local Development

```bash
# Create/activate venv (one-time)
python -m venv .venv
.venv/Scripts/activate          # Windows
source .venv/bin/activate       # Linux/Mac

pip install -r strategy_app/requirements.txt
pip install -r market_data_dashboard/requirements.txt
pip install pytest
```

Run tests before committing:

```bash
python -m pytest strategy_app/tests/ -q
python -m pytest market_data_dashboard/tests/ -q
```

### 2. Commit and Push

```bash
git add <files>
git commit -m "describe what and why"
git push origin <branch>
```

For changes that go to production, merge or push to `main`:

```bash
git checkout main
git merge --ff-only <branch>
git push origin main
```

### 3. Deploy to GCP

**Preferred path — always use the interactive lifecycle script:**

```bash
# From the repo root on the runtime VM, or via SSH:
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose:
- `1` → Bootstrap infra (first time only, or after infra changes)
- `2` → Start or restart runtime (normal deploy)
- `3` → Historical replay (never use this for live deploys)

**Manual sequence if you need it:**

```bash
# SSH in
gcloud compute ssh savitasajwan03@option-trading-runtime-01 --zone asia-south1-b --project amittrading-493606

# On the VM
cd /opt/option_trading
git pull --ff-only origin main
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --build strategy_app dashboard
```

See [`docs/runbooks/GCP_DEPLOYMENT.md`](docs/runbooks/GCP_DEPLOYMENT.md) for the full runbook.

### 4. Verify After Deploy

```bash
# Container health
gcloud compute ssh savitasajwan03@option-trading-runtime-01 --zone asia-south1-b --project amittrading-493606 \
  --command "sudo docker ps --format 'table {{.Names}}\t{{.Status}}'"

# Dashboard health
curl -fsS http://<vm-ip>:8008/api/health

# Strategy app logs (confirm engine started)
gcloud compute ssh savitasajwan03@option-trading-runtime-01 --zone asia-south1-b --project amittrading-493606 \
  --command "cd /opt/option_trading && sudo docker compose -f docker-compose.yml logs --tail 50 strategy_app"
```

Confirm in logs:
- `engine=ml_pure` (or `deterministic`)
- `model loaded` if ml_pure
- No `ERROR` at startup

---

## Model Switching

### Current model

`staged_simple_s2_v1` — research checkpoint. **Not production-ready** (all gates failed). Use `paper` rollout stage only.

```
gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/
├── model/model.joblib
├── config/profiles/ml_pure_staged_v1/threshold_report.json
└── reports/training/latest.json
```

### How to switch models

Edit `.env.compose`:

```env
STRATEGY_ENGINE=ml_pure
ML_PURE_MODEL_PACKAGE=gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/model/model.joblib
ML_PURE_THRESHOLD_REPORT=gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/config/profiles/ml_pure_staged_v1/threshold_report.json
STRATEGY_ROLLOUT_STAGE=paper    # use capped_live only when production guard criteria are met
```

GCS paths are downloaded automatically on first startup. See [`strategy_app/docs/README.md`](strategy_app/docs/README.md) for guard file requirements before using `capped_live`.

To switch back to deterministic:
```env
STRATEGY_ENGINE=deterministic
ML_PURE_MODEL_PACKAGE=
ML_PURE_THRESHOLD_REPORT=
```

### Dashboard model catalog

Set `GCS_MODEL_ROOTS` to show GCS-hosted models in the catalog UI:

```env
GCS_MODEL_ROOTS=gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1
```

---

## Health Checks

| Check | Command |
|---|---|
| Container status | `sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'` |
| Dashboard health | `curl -fsS http://localhost:8008/api/health` |
| Replay health | `curl -fsS http://localhost:8008/api/health/replay` |
| Strategy runtime | `curl -fsS http://localhost:8008/api/health/strategy-runtime` |
| Market data status | `curl -fsS http://localhost:8008/api/market-data/status` |
| Model catalog | `curl -fsS http://localhost:8008/api/trading/models` |

**snapshot_app is currently unhealthy.** To diagnose:

```bash
gcloud compute ssh savitasajwan03@option-trading-runtime-01 --zone asia-south1-b --project amittrading-493606 \
  --command "cd /opt/option_trading && sudo docker compose -f docker-compose.yml logs --tail 80 snapshot_app"
```

---

## Local Start (No Docker)

```bash
python -m start_apps --include-dashboard    # start all services
python -m stop_apps --include-dashboard     # stop all services
```

Or start strategy_app directly:

```bash
python -m strategy_app.main --engine deterministic
python -m strategy_app.main --engine ml_pure \
  --ml-pure-model-package gs://...model.joblib \
  --ml-pure-threshold-report gs://...threshold_report.json
```

---

## Key Configuration Files

| File | Purpose |
|---|---|
| `.env.compose` | All runtime env vars — the single source for compose deploys |
| `.env.compose.example` | Template — copy to `.env.compose` on a fresh checkout |
| `ops/gcp/operator.env` | GCP project/zone/VM names for ops scripts |
| `ops/gcp/operator.env.example` | Template for operator.env |
| `docker-compose.yml` | Base compose config (local build) |
| `docker-compose.gcp.yml` | GCP overlay (GHCR images, GCP-specific mounts) |

---

## Doc Index

### Start here
| Doc | What it covers |
|---|---|
| **This file** | System overview, daily workflow, deploy, health |
| [`docs/SYSTEM_SOURCE_OF_TRUTH.md`](docs/SYSTEM_SOURCE_OF_TRUTH.md) | Canonical rules — if docs conflict, this wins |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Component architecture and data flow |
| [`docs/PROCESS_TOPOLOGY.md`](docs/PROCESS_TOPOLOGY.md) | Service startup order, topic wiring, port map |

### Runbooks (operational steps)
| Doc | What it covers |
|---|---|
| [`docs/runbooks/README.md`](docs/runbooks/README.md) | Runbook index |
| [`docs/runbooks/GCP_DEPLOYMENT.md`](docs/runbooks/GCP_DEPLOYMENT.md) | **Live deploy and historical replay on GCP** |
| [`docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) | Building parquet datasets for training |
| [`docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`](docs/runbooks/TRAINING_RELEASE_RUNBOOK.md) | ML training → publish → deploy handoff |
| [`docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md`](docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md) | Rollback and environment cleanup |

### Strategy app
| Doc | What it covers |
|---|---|
| [`strategy_app/docs/README.md`](strategy_app/docs/README.md) | CLI args, env vars, engine modes, GCS model loading |
| [`strategy_app/docs/STRATEGY_ML_FLOW.md`](strategy_app/docs/STRATEGY_ML_FLOW.md) | Full snapshot → decision pipeline with diagrams |
| [`strategy_app/docs/OPERATOR_PLAYBOOK.md`](strategy_app/docs/OPERATOR_PLAYBOOK.md) | How to read monitoring output and alerts |
| [`strategy_app/docs/RELEASE_READINESS_CHECKLIST.md`](strategy_app/docs/RELEASE_READINESS_CHECKLIST.md) | Gate checklist before any production deploy |
| [`strategy_app/docs/IMPLEMENTATION_STATUS.md`](strategy_app/docs/IMPLEMENTATION_STATUS.md) | What is and isn't implemented |

### ML pipeline
| Doc | What it covers |
|---|---|
| [`ml_pipeline_2/docs/README.md`](ml_pipeline_2/docs/README.md) | Training pipeline overview |
| [`ml_pipeline_2/docs/MODEL_STATE_20260426.md`](ml_pipeline_2/docs/MODEL_STATE_20260426.md) | Current model research state and GCS paths |

### Dashboard
| Doc | What it covers |
|---|---|
| [`market_data_dashboard/README.md`](market_data_dashboard/README.md) | All endpoints, env vars, model catalog, GCS discovery |

### Historical record (do not update)
| Doc | Note |
|---|---|
| `strategy_app/docs/CURRENT_EVALUATION_BASELINE_2026-04-04.md` | Archived baseline |
| `strategy_app/docs/TECHNICAL_BRIEFING_CODE_REVIEW_2026-03-19.md` | Archived code review |
| `strategy_app/docs/code_review_2026-03-19.md` | Archived code review |

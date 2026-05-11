# BankNifty Options Algo — Operations Guide

**This is the starting point.** Read this first. Everything else is linked from here.

As-of: `2026-05-12`

---

## What This System Is

An end-to-end algorithmic trading runtime for BankNifty options on NSE. It ingests live market data, builds canonical snapshots, runs a 3-stage ML strategy, persists all events to MongoDB, and serves a monitoring dashboard.

The ML pipeline (`ml_pipeline_2`) trains offline (locally or on a training VM) and publishes model bundles to `ml_pipeline_2/artifacts/published_models/` (committed) and optionally to GCS. The live runtime loads the bundle at startup via `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP` (local-first; no GCS required for the Docker stack).

---

## Live State

**No GCP project is currently provisioned.** The previous project (`amittrading-493606`) is gone. All artifacts that matter — parquet training data, published models, runtime config, runtime guard — are backed up locally under the repo. Live trading and runtime VM hosting need a new GCP project (rebuild via [`docs/runbooks/RECOVERY_RUNBOOK.md`](docs/runbooks/RECOVERY_RUNBOOK.md)).

**Configured runtime (will become live once GCP is rebuilt):**

| Field | Value |
|---|---|
| Engine | `ml_pure` |
| Model run id | `staged_deep_hpo_c1_base_20260429_040848` (C1) |
| Model group | `banknifty_futures/h15_tp_auto` |
| Rollout stage | `capped_live` |
| Position size multiplier | `0.25` |
| Runtime guard | `.run/ml_runtime_guard_live.json` (`approved_for_runtime: true`) |
| Live PF (VOLATILE only) | `1.31` |
| Active runtime gate | `regime_gate_v1` (blocks `SIDEWAYS` and `AVOID` sessions) |

**Pending research:** E2 (`staged_dual_recipe.deep_hpo_e2_volatile_only.json`) — VOLATILE+SIDEWAYS regime-filtered S2 retraining; runs once GCP is rebuilt. See [`docs/runbooks/RECOVERY_RUNBOOK.md`](docs/runbooks/RECOVERY_RUNBOOK.md) and [`ml_pipeline_2/docs/training/INDEX.md`](ml_pipeline_2/docs/training/INDEX.md).

---

## Services

| Service | What it does | Dockerfile |
|---|---|---|
| `ingestion_app` | Live market data API, Kite session, live snapshot runner | `ingestion_app/Dockerfile` |
| `snapshot_app` | Builds canonical MarketSnapshot (MSS.1–MSS.9), publishes to Redis | `snapshot_app/Dockerfile` |
| `persistence_app` | Writes snapshots + strategy events to MongoDB | `persistence_app/Dockerfile` |
| `strategy_app` | Strategy engine (deterministic or ml_pure), emits TradeSignals | `strategy_app/Dockerfile` |
| `strategy_persistence_app` | Writes strategy votes/signals/positions to MongoDB | `persistence_app/Dockerfile` |
| `market_data_dashboard` | UI + monitoring APIs + model catalog + operator halt | `market_data_dashboard/Dockerfile` |
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
python -m pytest market_data_dashboard/test_*.py -q
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

### 3. Local Docker Stack (no GCP needed)

```bash
docker compose --env-file .env.compose up -d redis mongo dashboard strategy_eval_orchestrator
# Dashboard at http://localhost:8008
# Strategy eval UI at http://localhost:8011
```

For ml_pure live engine locally:

```bash
docker compose --env-file .env.compose up -d strategy_app
```

The strategy_app reads C1 from local `ml_pipeline_2/artifacts/published_models/` — no GCS required for replay or local research. Live trading still needs Kite credentials and (post-rebuild) a GCP runtime VM.

### 4. Deploy to GCP (when project is back)

After GCP rebuild via `bash ops/gcp/new_project_setup.sh`:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose:
- `1` → Bootstrap infra (first time, or after infra changes)
- `2` → Start or restart runtime (normal deploy)
- `3` → Historical replay (never use for live deploys)

See [`docs/runbooks/GCP_DEPLOYMENT.md`](docs/runbooks/GCP_DEPLOYMENT.md) for the full runbook and [`docs/runbooks/RECOVERY_RUNBOOK.md`](docs/runbooks/RECOVERY_RUNBOOK.md) for the post-GCP-loss recovery sequence.

### 5. Verify After Deploy

```bash
curl -fsS http://<vm-ip>:8008/api/health
curl -fsS http://<vm-ip>:8008/api/health/strategy-runtime   # rollout stage, run_id, hold rate
```

Confirm in strategy_app logs:
- `engine=ml_pure`
- `model loaded` with the expected `run_id`
- No `ERROR` at startup

---

## Model Switching

### Current configured model

`staged_deep_hpo_c1_base_20260429_040848` — published and approved for runtime. Loaded via run-id mode (no explicit `gs://` path needed).

```
ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/
├── data/training_runs/staged_deep_hpo_c1_base_20260429_040848/
│   ├── model/model.joblib
│   └── config/profiles/openfe_v9_dual/threshold_report.json
└── reports/training/run_staged_deep_hpo_c1_base_20260429_040848.json
```

### How to switch models

Edit `.env.compose`:

```env
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=<new_run_id>
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
STRATEGY_ROLLOUT_STAGE=capped_live   # paper | shadow | capped_live | live
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE=.run/ml_runtime_guard_live.json
```

The resolver looks up `ml_pipeline_2/artifacts/published_models/<MODEL_GROUP>/reports/training/run_<RUN_ID>.json` — no GCS download required. See [`strategy_app/docs/README.md`](strategy_app/docs/README.md) for guard file requirements before promoting beyond `paper`.

To halt the engine without restart:

```bash
curl -X POST http://localhost:8008/api/operator/halt
# … resume with:
curl -X DELETE http://localhost:8008/api/operator/halt
```

Halt creates a sentinel at `.run/strategy_app/operator_halt`. The risk manager polls it on every snapshot and blocks new entries while present.

---

## Health Checks

| Check | Command |
|---|---|
| Container status | `docker compose ps` |
| Dashboard health | `curl -fsS http://localhost:8008/api/health` |
| Strategy runtime | `curl -fsS http://localhost:8008/api/health/strategy-runtime` |
| Replay health | `curl -fsS http://localhost:8008/api/health/replay` |
| Market data status | `curl -fsS http://localhost:8008/api/market-data/status` |
| Model catalog | `curl -fsS http://localhost:8008/api/trading/models` |
| Halt status | `curl -fsS http://localhost:8008/api/operator/halt` |

---

## Local Start (No Docker)

```bash
python -m start_apps --include-dashboard    # start all services
python -m stop_apps --include-dashboard     # stop all services
```

Or start strategy_app directly:

```bash
python -m strategy_app.main --engine ml_pure \
  --ml-pure-run-id staged_deep_hpo_c1_base_20260429_040848 \
  --ml-pure-model-group banknifty_futures/h15_tp_auto \
  --rollout-stage capped_live \
  --position-size-multiplier 0.25 \
  --ml-runtime-guard-file .run/ml_runtime_guard_live.json
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
| `.run/ml_runtime_guard_live.json` | Runtime safety gate (approved_for_runtime, paper/shadow day counts) |

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
| [`docs/runbooks/RECOVERY_RUNBOOK.md`](docs/runbooks/RECOVERY_RUNBOOK.md) | **Post-GCP-loss recovery** — local training, new GCP rebuild, offline replay |
| [`docs/runbooks/LIVE_SETUP_GUIDE.md`](docs/runbooks/LIVE_SETUP_GUIDE.md) | Live runtime bring-up checklist |
| [`docs/runbooks/GCP_DEPLOYMENT.md`](docs/runbooks/GCP_DEPLOYMENT.md) | Live deploy and historical replay on GCP |
| [`docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md) | Building parquet datasets for training |
| [`docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`](docs/runbooks/TRAINING_RELEASE_RUNBOOK.md) | ML training → publish → deploy handoff |
| [`docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md`](docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md) | Rollback and environment cleanup |
| [`docs/runbooks/DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md`](docs/runbooks/DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md) | Historical replay walkthrough |

### Strategy app
| Doc | What it covers |
|---|---|
| [`strategy_app/docs/README.md`](strategy_app/docs/README.md) | CLI args, env vars, engine modes, model loading |
| [`strategy_app/docs/STRATEGY_ML_FLOW.md`](strategy_app/docs/STRATEGY_ML_FLOW.md) | Full snapshot → decision pipeline with diagrams |
| [`strategy_app/docs/OPERATOR_PLAYBOOK.md`](strategy_app/docs/OPERATOR_PLAYBOOK.md) | How to read monitoring output and alerts |
| [`strategy_app/docs/RELEASE_READINESS_CHECKLIST.md`](strategy_app/docs/RELEASE_READINESS_CHECKLIST.md) | Gate checklist before any production deploy |
| [`strategy_app/docs/IMPLEMENTATION_STATUS.md`](strategy_app/docs/IMPLEMENTATION_STATUS.md) | What is and isn't implemented |

### ML pipeline
| Doc | What it covers |
|---|---|
| [`ml_pipeline_2/docs/README.md`](ml_pipeline_2/docs/README.md) | Training pipeline overview |
| [`ml_pipeline_2/docs/training/INDEX.md`](ml_pipeline_2/docs/training/INDEX.md) | Training run history, resume commands, grid status |
| [`ml_pipeline_2/docs/training/MODEL_STATE_20260502.md`](ml_pipeline_2/docs/training/MODEL_STATE_20260502.md) | Current model research state (D2 regime breakdown, E1→E2) |

### Dashboard
| Doc | What it covers |
|---|---|
| [`market_data_dashboard/README.md`](market_data_dashboard/README.md) | All endpoints, env vars, model catalog, operator halt |

### Historical record (do not update)
| Doc | Note |
|---|---|
| `strategy_app/docs/CURRENT_EVALUATION_BASELINE_2026-04-04.md` | Archived baseline |
| `strategy_app/docs/TECHNICAL_BRIEFING_CODE_REVIEW_2026-03-19.md` | Archived code review |
| `strategy_app/docs/code_review_2026-03-19.md` | Archived code review |
| `ml_pipeline_2/docs/training/MODEL_STATE_20260426.md` | Pre-C1 research snapshot |
| `ml_pipeline_2/docs/training/MODEL_STATE_20260428.md` | Pre-C1 research snapshot |

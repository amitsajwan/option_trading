# GCP Loss Recovery Runbook

Use this when your GCP project has expired, been deleted, or is otherwise unavailable.

**Good news:** everything that matters is local. Models, training data, pipeline code, and runtime config are all in the repo or the local backup. GCP is only required for live trading and cloud-hosted VMs.

---

## What You Have Locally (Nothing Critical Is Lost)

| Asset | Location | Status |
|-------|----------|--------|
| Training parquet data (3.1GB) | `.data/ml_pipeline/parquet_data/` | ✅ Complete — all 4 training views + v2/v3 |
| Published model (C1) | `ml_pipeline_2/artifacts/published_models/` | ✅ Ready for inference |
| Research run artifacts | `ml_pipeline_2/artifacts/research/` | ✅ C1, D2, E1 + key runs |
| Runtime config | `.deploy/runtime-config/` | ✅ Last known good config |
| Runtime guard | `.run/ml_runtime_guard_live.json` | ✅ `approved_for_runtime: true` |
| All pipeline code | `ml_pipeline_2/` | ✅ E2 fixes committed |
| Docker Compose stack | `docker-compose.yml` | ✅ Runs fully offline |

**What you cannot do without GCP:** live trading (Kite credential sync), runtime VM hosting.

---

## Option A: Run Training Locally (No GCP Needed)

Training runs entirely on local parquet data. No GCS, no VM.

```bash
cd <repo_root>

# E2 — VOLATILE+SIDEWAYS regime-filtered S2 training (the pending run)
PYTHONPATH=. .venv/Scripts/python.exe -u -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json \
  > ml_pipeline_2/tools/e2_local_run.log 2>&1 &

# Monitor progress
watch -n 30 "cat ml_pipeline_2/artifacts/research/staged_deep_hpo_e2_volatile_only_*/run_status.json"
```

**On Linux/Mac:** replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

Expected duration: 4–8 hours (HPO with 80 S2 + 50 S3 experiments, 4-core machine).

---

## Option B: Rebuild on a New GCP Project

### One-command rebuild (45 minutes)

```bash
NEW_PROJECT=your-new-project-id bash ops/gcp/new_project_setup.sh
```

This script handles:
1. Updates `operator.env` with new project ID
2. Enables required GCP APIs
3. Runs Terraform (VMs, buckets, networking, service accounts)
4. Uploads local parquet + models to new GCS buckets
5. Creates training VM

### Manual step-by-step

**Step 1: New GCP project setup (5 min)**
```bash
# Create project at console.cloud.google.com or:
gcloud projects create YOUR_PROJECT_ID --name="Option Trading"
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login
```

**Step 2: Update operator.env (2 min)**
```bash
OLD="amittrading-493606"
NEW="your-new-project-id"
sed -i "s/${OLD}/${NEW}/g" ops/gcp/operator.env
sed -i 's/IMAGE_SOURCE=.*/IMAGE_SOURCE="ghcr"/' ops/gcp/operator.env
```

**Step 3: Bootstrap infrastructure (15 min)**
```bash
RUN_TERRAFORM=1 RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 \
  TERRAFORM_AUTO_APPROVE=1 \
  bash ops/gcp/from_scratch_bootstrap.sh
```

**Step 4: Upload local data to new buckets (10 min)**
```bash
source ops/gcp/operator.env

# Parquet data (~3.1GB)
gcloud storage rsync .data/ml_pipeline/parquet_data \
  "gs://${SNAPSHOT_DATA_BUCKET_NAME}/ml_pipeline/parquet_data" --recursive

# Published models (~14MB)
gcloud storage rsync ml_pipeline_2/artifacts/published_models \
  "gs://${MODEL_BUCKET_NAME}/published_models" --recursive

# Runtime config
gcloud storage rsync .deploy/runtime-config \
  "gs://${RUNTIME_CONFIG_BUCKET_NAME}/runtime" --recursive
```

**Step 5: Create training VM and run E2 (10 min setup + 4-8h training)**
```bash
bash ops/gcp/create_training_vm.sh

# SSH to VM
gcloud compute ssh savitasajwan03@option-trading-ml-01 \
  --zone=asia-south1-b --project=YOUR_PROJECT_ID

# On VM
cd ~/option_trading && git pull
tmux new -s e2
PYTHONPATH=. .venv/bin/python -u -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json \
  2>&1 | tee ml_pipeline_2/tools/e2_run.log
```

---

## Option C: Local Docker Stack (Offline Research / Replay)

The full Docker Compose stack runs offline — no GCS access required.

```bash
# Start local stack (Redis, MongoDB, dashboard, strategy_eval)
docker compose up -d redis mongo dashboard strategy_eval_orchestrator

# Dashboard at http://localhost:8008
# Strategy eval UI at http://localhost:8011

# Run historical replay (uses local parquet in .data/)
# From dashboard UI: Historical → select date range → run
```

The `STRATEGY_ENGINE=ml_pure` is configured in `.env.compose` with C1 model loading
from `ml_pipeline_2/artifacts/published_models/` — no GCS needed.

---

## After Training Completes

### If E2 passes all gates (auto-publish path)
```bash
# Check results
cat ml_pipeline_2/artifacts/research/staged_deep_hpo_e2_volatile_only_*/summary.json \
  | python3 -m json.tool | grep -E '"decision|blocking_reasons|profit_factor|block_rate"'

# Publish to local artifacts
PYTHONPATH=. python3 -m ml_pipeline_2.staged.publish_research_run \
  --run-dir ml_pipeline_2/artifacts/research/staged_deep_hpo_e2_volatile_only_<TIMESTAMP>

# Update .env.compose to point to E2
sed -i 's/ML_PURE_RUN_ID=.*/ML_PURE_RUN_ID=staged_deep_hpo_e2_volatile_only_<TIMESTAMP>/' .env.compose
```

### If E2 passes VOLATILE PF ≥ 1.3 but is HELD (force-deploy path)
```bash
# Force-deploy bypasses combined gate — use only when VOLATILE regime edge is confirmed
RUN_DIR=ml_pipeline_2/artifacts/research/staged_deep_hpo_e2_volatile_only_<TIMESTAMP> \
MODEL_GROUP=banknifty_futures/h15_tp_auto \
PROFILE_ID=openfe_v9_dual \
bash ops/gcp/force_deploy_research_run.sh
```

---

## Key Facts to Keep in Mind

- **C1 is still live** (`staged_deep_hpo_c1_base_20260429_040848`) — VOLATILE PF=1.31, `regime_gate_v1` active
- **E2 is the next candidate** — same hypothesis as E1 but with pipeline bug fixed (stage2 regime column enrichment)
- **Pipeline fixes** are in `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py` (Fix 1 + Fix 2, committed)
- **E2 config** is at `ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json`
- **Local training** is the primary path — no GCP required for research
- **GCP** is only required for: Kite credential sync, production runtime VM hosting

---

## Quick Reference: What's Where

```
ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848/  ← live model run
ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/    ← published C1
ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json  ← E2 config
.data/ml_pipeline/parquet_data/                                             ← training data
.run/ml_runtime_guard_live.json                                             ← runtime guard
.env.compose                                                                ← Docker config
ops/gcp/operator.env                                                        ← GCP config
ops/gcp/new_project_setup.sh                                               ← one-cmd rebuild
```

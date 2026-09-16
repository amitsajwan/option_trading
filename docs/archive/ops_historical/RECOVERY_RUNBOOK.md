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

**What you cannot do without GCP:** runtime VM hosting (live trading container, market-data ingestion). Kite credentials themselves are NOT in GCP — they live on your operator machine and are pushed up by `start_runtime_interactive.sh`. See "Kite credentials" below.

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

`REPO_REF` in `ops/gcp/operator.env` controls which branch the VM clones — confirm it is `"main"`.

`create_training_vm.sh` automatically tries all three zones (`a`, `b`, `c`) in the region if your configured zone is exhausted. No manual intervention needed.

```bash
bash ops/gcp/create_training_vm.sh
```

The VM startup script clones the repo, sets up the venv, and syncs parquet data from GCS automatically. Wait ~5 minutes for it to finish, then SSH in to start training:

```bash
# SSH to VM — use your gcloud OS Login account (run `gcloud auth list` to check)
gcloud compute ssh YOUR_ACCOUNT@option-trading-ml-01 \
  --zone=asia-south1-b --project=YOUR_PROJECT_ID

# On VM: patch C1 resolved_config.json parquet_root to the VM's absolute path.
# (The compatibility check in stage1_reuse compares parquet_root after resolution,
#  which absolutizes it — the stored relative path from the old VM won't match.)
cd /opt/option_trading
python3 -c "
import json; from pathlib import Path
p = Path('ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848/resolved_config.json')
d = json.loads(p.read_text())
d['inputs']['parquet_root'] = str(Path('/opt/option_trading/.data/ml_pipeline/parquet_data'))
p.write_text(json.dumps(d, indent=2))
print('patched:', d['inputs']['parquet_root'])
"

# Start E2 training in tmux (survives SSH disconnect)
tmux new -s e2
PYTHONPATH=. .venv/bin/python -u -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json \
  2>&1 | tee ml_pipeline_2/tools/e2_run.log

# Detach from tmux: Ctrl+B then D
# Re-attach later: tmux attach -t e2
```

**If the startup script failed** (check `/var/log/option-trading-training-startup.log`), run setup manually:
```bash
# On VM
sudo bash -s << 'EOF'
git clone https://github.com/amitsajwan/option_trading.git /opt/option_trading
cd /opt/option_trading && git checkout main
python3 -m venv .venv && .venv/bin/pip install -e ./ml_pipeline_2 --quiet
gcloud storage rsync gs://YOUR_PROJECT-option-trading-snapshots/ml_pipeline \
  /opt/option_trading/.data/ml_pipeline --recursive --project YOUR_PROJECT
chown -R ubuntu:ubuntu /opt/option_trading
EOF
```

**Step 6: Restore Kite credentials (for live trading only)**

Kite credentials are NOT backed up to GCS — they live on your operator machine. After the new project is up:

```bash
# Fill kite keys in operator.env (api_key + api_secret only, NOT access_token)
sed -i 's/^KITE_API_KEY=.*/KITE_API_KEY="your_api_key"/' ops/gcp/operator.env
sed -i 's/^KITE_API_SECRET=.*/KITE_API_SECRET="your_api_secret"/' ops/gcp/operator.env

# Refresh access token via local interactive auth flow (opens Zerodha login in browser)
python3 -m ingestion_app.kite_auth --force

# Sync credentials.json to runtime VM via start_runtime_interactive.sh, which mounts
# secrets via the runtime config bucket — see TRAINING_RELEASE_RUNBOOK.md §"Start runtime"
bash ops/gcp/start_runtime_interactive.sh
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

# Publish to local artifacts (writes to ml_pipeline_2/artifacts/published_models/)
PYTHONPATH=. python3 -m ml_pipeline_2.run_staged_release \
  --run-dir ml_pipeline_2/artifacts/research/staged_deep_hpo_e2_volatile_only_<TIMESTAMP> \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual

# Update .env.compose to point to E2
sed -i 's/ML_PURE_RUN_ID=.*/ML_PURE_RUN_ID=staged_deep_hpo_e2_volatile_only_<TIMESTAMP>/' .env.compose
```

**For local Docker / replay only:** publishing is optional — local Docker reads research-run artifacts directly via `ML_PURE_RUN_ID` resolution against `ml_pipeline_2/artifacts/published_models/`. You only need to publish when you intend to deploy to a GCP runtime VM.

### If E2 passes VOLATILE PF ≥ 1.3 but is HELD (force-deploy to GCP runtime)
```bash
# Force-deploy bypasses combined gate — use only when VOLATILE regime edge is confirmed.
# On a NEW project, you MUST override the bucket URLs (defaults point at the old
# amittrading-493606 buckets which no longer exist).
source ops/gcp/operator.env

RUN_DIR=ml_pipeline_2/artifacts/research/staged_deep_hpo_e2_volatile_only_<TIMESTAMP> \
MODEL_GROUP=banknifty_futures/h15_tp_auto \
PROFILE_ID=openfe_v9_dual \
APP_IMAGE_TAG=latest \
MODEL_BUCKET_URL="gs://${MODEL_BUCKET_NAME}/published_models" \
RUNTIME_CONFIG_BUCKET_URL="gs://${RUNTIME_CONFIG_BUCKET_NAME}/runtime" \
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

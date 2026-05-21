---
name: gcp-vm-deploy
description: Deploy this option trading project to local Google Cloud Compute Engine VMs using gcloud SSH/SCP. Use when user asks to deploy to ML or runtime VM, redeploy code, restart services, verify VM health, or run remote commands on GCP.
---

# GCP VM Deploy Skill

Use this skill when the user asks to deploy, redeploy, restart, verify, or check this project on Google Cloud VM.

## GCP project

Project ID:

```txt
algo-trading-496203
```

Default zone:

```txt
asia-south1-b
```

Set once per operator session (from repo root):

```bash
export PROJECT_ID=algo-trading-496203
export ZONE=asia-south1-b
gcloud config set project "${PROJECT_ID}"
gcloud config set compute/zone "${ZONE}"
```

## Deployment targets

### ML target

Use for:

- research
- backtesting
- historical data processing
- model development
- experiments
- heavy compute jobs

VM:

```txt
option-trading-ml-01
```

Remote directory:

```txt
~/option-trading-ml
```

### Runtime target

Use for:

- production runtime
- FastAPI service
- WebSocket service
- scheduler
- live option-chain fetcher
- trading engine
- broker API integration
- monitoring dashboard

VM:

```txt
option-trading-runtime-01
```

Remote directory:

```txt
~/option-trading-runtime
```

## Choose target first

| User intent | Target | VM | Remote dir |
|-------------|--------|-----|------------|
| training, HPO, parquet build, research artifacts | ML | `option-trading-ml-01` | `~/option-trading-ml` |
| live stack, dashboard, Kite, compose services | Runtime | `option-trading-runtime-01` | `~/option-trading-runtime` |

If unclear, ask which lane (ML vs runtime) before running destructive commands.

## Preflight (always)

Run from the operator machine (WSL, Linux, or Windows with `gcloud`):

```bash
gcloud auth list
gcloud compute instances describe option-trading-ml-01 --project="${PROJECT_ID}" --zone="${ZONE}" --format="value(status)"
gcloud compute instances describe option-trading-runtime-01 --project="${PROJECT_ID}" --zone="${ZONE}" --format="value(status)"
```

Both should be `RUNNING` before deploy. If `TERMINATED`, start the VM:

```bash
gcloud compute instances start <VM_NAME> --project="${PROJECT_ID}" --zone="${ZONE}"
```

Confirm remote checkout exists:

```bash
gcloud compute ssh <VM_NAME> --project="${PROJECT_ID}" --zone="${ZONE}" \
  --command "test -d ~/option-trading-ml && echo ml_ok; test -d ~/option-trading-runtime && echo rt_ok; ls -la ~ | grep option-trading"
```

Adjust the path check to the target VM only.

## SSH and SCP primitives

SSH (interactive shell):

```bash
gcloud compute ssh <VM_NAME> --project="${PROJECT_ID}" --zone="${ZONE}"
```

SSH (one command):

```bash
gcloud compute ssh <VM_NAME> --project="${PROJECT_ID}" --zone="${ZONE}" \
  --command "cd ~/option-trading-ml && pwd"   # or ~/option-trading-runtime
```

SCP file or directory to VM:

```bash
gcloud compute scp --recurse \
  <local-path> \
  <VM_NAME>:<remote-path> \
  --project="${PROJECT_ID}" --zone="${ZONE}"
```

Example — sync one changed module to ML VM:

```bash
gcloud compute scp --recurse \
  ml_pipeline_2/src/ml_pipeline_2/training/foo.py \
  option-trading-ml-01:~/option-trading-ml/ml_pipeline_2/src/ml_pipeline_2/training/foo.py \
  --project="${PROJECT_ID}" --zone="${ZONE}"
```

**Do not use `gcloud compute scp` for application source.** Commit → push → VM `git pull` → docker build. SCP is only for data artifacts or git emergencies.

## Code deploy workflows

### A. Git push + pull on VM (required default)

**Operator machine:**

```bash
git push origin main   # after local commit
```

**Then on each VM:**

```bash
REMOTE=/opt/option_trading   # legacy: ~/option-trading-ml or ~/option-trading-runtime
BRANCH=main

gcloud compute ssh <VM_NAME> --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  sudo bash -c 'set -e
  cd ${REMOTE}
  git fetch origin ${BRANCH}
  git checkout ${BRANCH}
  git pull --ff-only origin ${BRANCH}
  git log -1 --oneline'
"
```

**Runtime — rebuild after pull** (when `strategy_app`, `market_data_dashboard`, or `ml_pipeline_2` in image changed):

```bash
gcloud compute ssh option-trading-runtime-01 --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  cd /opt/option_trading
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
    build strategy_app_historical
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate strategy_app_historical
"
```

Add `strategy_app`, `market_data_dashboard`, etc. to `build` / `up` as needed.

### B. Rsync / SCP (avoid — no git on VM or emergency only)

From repo root, exclude heavy dirs. Example tarball push:

```bash
TARGET_VM=option-trading-ml-01
REMOTE=~/option-trading-ml

tar czf /tmp/option_trading_sync.tgz \
  --exclude='.git' --exclude='.data' --exclude='.venv' \
  --exclude='ml_pipeline_2/artifacts/research' \
  --exclude='__pycache__' \
  -C . .

gcloud compute scp /tmp/option_trading_sync.tgz \
  "${TARGET_VM}:${REMOTE}/../option_trading_sync.tgz" \
  --project="${PROJECT_ID}" --zone="${ZONE}"

gcloud compute ssh "${TARGET_VM}" --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  set -e
  cd ${REMOTE}
  tar xzf ../option_trading_sync.tgz
  rm -f ../option_trading_sync.tgz
"
```

Prefer fixing git on the VM and using workflow A. SCP single files only in emergencies.

### C. Runtime — operator lifecycle (live / historical)

For production runtime deploys, prefer repo scripts over hand-rolled compose:

```bash
cp -n ops/gcp/operator.env.example ops/gcp/operator.env   # if missing
# Edit ops/gcp/operator.env: PROJECT_ID, ZONE, RUNTIME_NAME=option-trading-runtime-01

bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Menu mapping:

- `1` — Bootstrap infra
- `2` — Start/restart live runtime (publishes runtime config, Kite preflight, VM restart)
- `3` — Historical replay

See [docs/runbooks/GCP_DEPLOYMENT.md](../../docs/runbooks/GCP_DEPLOYMENT.md) and [reference.md](reference.md) for bucket URLs and verification commands.

### D. Runtime — quick VM-side compose (already on VM)

When SSH'd into runtime VM and iterating with `IMAGE_SOURCE=local_build`:

```bash
cd ~/option-trading-runtime
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml build strategy_app
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml up -d
```

See [ops/gcp/VELOCITY_RUNTIME_DEPLOY.md](../../ops/gcp/VELOCITY_RUNTIME_DEPLOY.md) for a focused local-build example.

### E. ML — remote training / jobs

After code is on the ML VM:

```bash
gcloud compute ssh option-trading-ml-01 --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  cd ~/option-trading-ml
  # example: check running training
  pgrep -af 'ml_pipeline_2|python.*train' || true
  ls -lt ml_pipeline_2/artifacts/research 2>/dev/null | head
"
```

Use `bash ./ops/gcp/start_training_interactive.sh` from the operator checkout for staged training/release flows.

## Verify after deploy

### ML VM

```bash
gcloud compute ssh option-trading-ml-01 --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  cd ~/option-trading-ml && git log -1 --oneline && du -sh .data/ml_pipeline 2>/dev/null || echo 'no .data yet'
"
```

### Runtime VM

```bash
gcloud compute ssh option-trading-runtime-01 --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  sudo tail -n 80 /var/log/option-trading-runtime-startup.log 2>/dev/null || true
  cd ~/option-trading-runtime && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps
  curl -fsS http://127.0.0.1:8008/api/health 2>/dev/null || echo 'dashboard not up'
"
```

If compose fails, check whether checkout is under `~/option-trading-runtime` vs legacy `/opt/option_trading` — use the path that exists on the VM.

## Restart runtime VM (config pull on boot)

```bash
gcloud compute instances stop option-trading-runtime-01 --project="${PROJECT_ID}" --zone="${ZONE}"
gcloud compute instances start option-trading-runtime-01 --project="${PROJECT_ID}" --zone="${ZONE}"
```

Only after publishing runtime config when the change requires startup sync from GCS.

## Safety rules

- Never SCP secrets (`.env` with keys, `credentials.json`) unless the user explicitly requests it.
- Do not restart the runtime VM during market hours without user approval.
- ML and runtime are separate checkouts — do not assume paths are interchangeable.
- Prefer `ops/gcp/` scripts for live/historical; use git pull + compose rebuild for deploys, not SCP.
- On Windows, run `gcloud` from PowerShell or WSL; long-running training should use `nohup` or `tmux` on the VM.

## Additional resources

- GCS buckets and legacy paths: [reference.md](reference.md)
- Full operator runbook: [docs/runbooks/GCP_DEPLOYMENT.md](../../docs/runbooks/GCP_DEPLOYMENT.md)
- Terraform values: [infra/gcp/terraform.tfvars](../../infra/gcp/terraform.tfvars)

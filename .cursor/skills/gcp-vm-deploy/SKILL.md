---
name: gcp-vm-deploy
description: Deploy this option trading project to Google Cloud Compute Engine VMs using gcloud SSH and git pull (no SCP for source). Use for unified VM, legacy ML/runtime VMs, deploy, restart, verify health, or remote training jobs.
---

# GCP VM Deploy Skill

Use this skill when the user asks to deploy, redeploy, restart, verify, or run jobs on GCP.

## GCP project

```txt
Project: algo-trading-496203
Zone:    asia-south1-b
```

```bash
export PROJECT_ID=algo-trading-496203
export ZONE=asia-south1-b
gcloud config set project "${PROJECT_ID}"
gcloud config set compute/zone "${ZONE}"
```

## Deployment target (preferred: unified)

**One VM for runtime + ML** — see [docs/GCP_UNIFIED_VM.md](../../docs/GCP_UNIFIED_VM.md).

| Field | Value |
|-------|--------|
| VM name | `option-trading-01` (target) or keep `option-trading-runtime-01` after merge |
| Machine type | **`e2-highmem-16`** (16 vCPU, 128 GB) — trial-friendly in `asia-south1-b` |
| Checkout | `/opt/option_trading` |
| Fallback type | `e2-standard-16` (64 GB) if highmem unavailable |

**Do not** run heavy ML (oracle/HPO) during live market hours on the same host without stopping extra compose profiles or explicit user approval.

### Legacy dual-VM (until merged)

| Lane | VM | Path |
|------|-----|------|
| Runtime | `option-trading-runtime-01` | `/opt/option_trading` |
| ML | `option-trading-ml-01` | `/opt/option_trading` |

`n2-highmem-*` may return `ZONE_RESOURCE_POOL_EXHAUSTED` in this zone; prefer **E2 highmem** (`e2-highmem-8`, `e2-highmem-16`).

## Choose target

| User intent | Target |
|-------------|--------|
| Live stack, dashboard, Kite, compose | Unified / runtime VM |
| Training, HPO, parquet, research | Same VM (off-hours) or legacy `option-trading-ml-01` |
| Both | **Unified VM only** after migration |

Default `VM_NAME` for scripts: `option-trading-runtime-01` or `option-trading-01` — use whichever exists:

```bash
gcloud compute instances list --project="${PROJECT_ID}" --filter="name~option-trading"
```

## Preflight

```bash
gcloud compute instances describe "${VM_NAME}" \
  --project="${PROJECT_ID}" --zone="${ZONE}" \
  --format="table(name,machineType.basename(),status)"
```

Start if stopped:

```bash
gcloud compute instances start "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}"
```

Check RAM (expect ≥64 Gi on unified):

```bash
gcloud compute ssh "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" \
  --command "free -h && nproc"
```

## Code deploy (required: git, not SCP)

**Local:** commit → `git push origin main`

**VM:**

```bash
gcloud compute ssh "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  sudo bash -c 'cd /opt/option_trading &&
    git fetch origin main && git checkout main && git pull --ff-only origin main &&
    git log -1 --oneline'
"
```

**Runtime rebuild** (when `strategy_app`, `market_data_dashboard`, compose paths changed):

`docker-compose.gcp.yml` uses `pull_policy: always` on `strategy_app` / `persistence_app` images. After `build`, you **must** pass **`--pull never`** on `up` or Compose will pull an old GHCR tag and the container will **not** include the code from `git pull`.

```bash
gcloud compute ssh "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  cd /opt/option_trading
  sudo docker compose --env-file .env.compose \
    -f docker-compose.yml -f docker-compose.gcp.yml \
    build strategy_app_historical
  sudo docker compose --env-file .env.compose \
    -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate --pull never strategy_app_historical
"
```

**Post-deploy check** (second `strategy router configured` line must match `STRATEGY_PROFILE_ID` / expected profile):

```bash
gcloud compute ssh "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --command "
  sudo docker logs option_trading-strategy_app_historical-1 2>&1 | grep 'strategy router configured' | tail -2
"
```

If you only see `det_prod_v1` once and never the target profile, the wrong image is running — rebuild with `--pull never`.

**Historical replay:** start `strategy_app_historical` and wait for `strategy consumer subscribed` before launching replay (pub/sub is not buffered). If consumer lock is stale after recreate:

```bash
sudo docker exec option_trading-redis-1 redis-cli DEL strategy_app:consumer_lock:market:snapshot:v1:historical
```

## ML jobs on unified VM

After `git pull`, use venv (no docker rebuild unless deps changed):

```bash
# Direction HPO (hours; off-hours only on unified host)
sudo bash /opt/option_trading/ops/gcp/run_direction_only_hpo_vm.sh

# Status
sudo bash /opt/option_trading/ops/gcp/run_direction_only_hpo_vm.sh status
```

Parquet root: `/opt/option_trading/.data/ml_pipeline/parquet_data`

## Resize VM (more RAM/CPU)

```bash
gcloud compute instances stop "${VM_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"
gcloud compute instances set-machine-type "${VM_NAME}" \
  --machine-type=e2-highmem-16 --zone="${ZONE}" --project="${PROJECT_ID}"
gcloud compute instances start "${VM_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"
```

## Verify

```bash
# Health
gcloud compute ssh "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" \
  --command "curl -fsS http://127.0.0.1:8008/api/health; sudo docker compose -f /opt/option_trading/docker-compose.yml ps 2>/dev/null | head -15"

# ML artifacts
gcloud compute ssh "${VM_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" \
  --command "ls -lt /opt/option_trading/ml_pipeline_2/artifacts/research 2>/dev/null | head -5"
```

## Safety rules

- Never SCP application source; git only.
- Never SCP secrets unless user explicitly requests.
- **Always `up --pull never`** after `build` when using `docker-compose.gcp.yml` on the VM.
- Do not restart runtime during market hours without approval.
- On unified VM: avoid concurrent full compose + oracle/HPO without ≥64 GB RAM and scheduling.
- ML VM `option-trading-ml-01`: stop after unified cutover to save cost.

## References

- Unified VM analysis: [docs/GCP_UNIFIED_VM.md](../../docs/GCP_UNIFIED_VM.md)
- Operator runbook: [docs/runbooks/GCP_DEPLOYMENT.md](../../docs/runbooks/GCP_DEPLOYMENT.md)
- GCS paths: [reference.md](reference.md)

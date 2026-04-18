# Velocity Deployment Guide

The supported GCP runtime deployment for this repository is the VM + Docker Compose flow under `ops/gcp`. The Cloud Run command sequence is not valid for the live stack because the runtime depends on Compose services, mounted runtime config, Redis, Mongo, Kite credentials, and published model artifacts.

Use the canonical runbook:

```bash
ops/gcp/VELOCITY_RUNTIME_DEPLOY.md
```

## Already on the Runtime VM

```bash
cd /opt/option_trading
bash ./quick_deploy_gcp.sh
```

The helper does the VM-safe path:

- pulls `main`
- ensures `.env.compose` exists
- sets `STRATEGY_ENHANCED_VELOCITY=1`
- sets `IMAGE_SOURCE=local_build`
- runs `strategy_app/engines/test_velocity_policies.py`
- rebuilds `strategy_app` and `dashboard`
- starts them with Docker Compose and the `ui` profile

## Manual Commands

```bash
cd /opt/option_trading

git fetch origin main
git checkout main
git pull --ff-only origin main

test -f .env.compose || cp .env.compose.example .env.compose

if grep -q '^STRATEGY_ENHANCED_VELOCITY=' .env.compose; then
  sed -i 's/^STRATEGY_ENHANCED_VELOCITY=.*/STRATEGY_ENHANCED_VELOCITY=1/' .env.compose
else
  echo 'STRATEGY_ENHANCED_VELOCITY=1' >> .env.compose
fi

if grep -q '^IMAGE_SOURCE=' .env.compose; then
  sed -i 's/^IMAGE_SOURCE=.*/IMAGE_SOURCE=local_build/' .env.compose
else
  echo 'IMAGE_SOURCE=local_build' >> .env.compose
fi

python3 -m pip install pytest pandas numpy -q
python3 -m pytest strategy_app/engines/test_velocity_policies.py -q

sudo docker compose --env-file .env.compose -f docker-compose.yml build strategy_app dashboard
sudo docker compose --env-file .env.compose -f docker-compose.yml --profile ui up -d strategy_app dashboard
sudo docker compose --env-file .env.compose -f docker-compose.yml --profile ui ps
```

## Verify

```bash
curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8008}/api/health" | python3 -m json.tool

curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8008}/api/trading/velocity-testing/test" \
  -G \
  --data-urlencode "date_from=2026-04-11" \
  --data-urlencode "date_to=2026-04-18" \
  --data-urlencode "trade_direction=CE" | python3 -m json.tool
```

Dashboard:

```text
http://<runtime-vm-external-ip>:8008/trading/velocity-testing
```

## Operator Flow

From an operator checkout:

```bash
cd /path/to/option_trading
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose `2) Start/restart runtime`.

If `IMAGE_SOURCE=ghcr`, publish images containing the velocity changes first and update `APP_IMAGE_TAG` in `.env.compose`. If `IMAGE_SOURCE=local_build`, the runtime VM builds the current checkout.

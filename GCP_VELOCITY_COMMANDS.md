# GCP Velocity Deployment Commands

This repo deploys the runtime stack on a GCP VM with Docker Compose and the scripts in `ops/gcp`. Do not use `gcloud run deploy` for this stack.

## Fast Path: Already on the Runtime VM

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
cd /opt/option_trading

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

## One Command Helper

```bash
cd /opt/option_trading
bash ./quick_deploy_gcp.sh
```

Set `RUN_GIT_PULL=0` or `RUN_TESTS=0` to skip those phases.

## Normal Operator Path

From an operator checkout, use:

```bash
cd /path/to/option_trading
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose `2) Start/restart runtime`.

The operator flow publishes `.env.compose` to `RUNTIME_CONFIG_BUCKET_URL` and restarts the VM. If you use `IMAGE_SOURCE=ghcr`, make sure `APP_IMAGE_TAG` points to a published image tag containing the velocity changes. If you use `IMAGE_SOURCE=local_build`, the runtime VM builds from its checkout.

More detail: `ops/gcp/VELOCITY_RUNTIME_DEPLOY.md`.

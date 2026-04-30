# Velocity Runtime Deploy on GCP VM

Use this when you are already on the GCP runtime VM and want to deploy the current checkout through the repo's Docker Compose runtime path. This repo does not deploy the live stack with Cloud Run.

The runtime VM checkout is normally:

```bash
/opt/option_trading
```

## Immediate VM Deploy

```bash
cd /opt/option_trading

# Get the code that contains the velocity changes.
sudo git fetch origin main
sudo git checkout main
sudo git pull --ff-only origin main

# Make sure the runtime env exists.
test -f .env.compose || sudo cp .env.compose.example .env.compose

# Enable the velocity deterministic policy path for Compose.
if grep -q '^STRATEGY_ENHANCED_VELOCITY=' .env.compose; then
  sudo sed -i 's/^STRATEGY_ENHANCED_VELOCITY=.*/STRATEGY_ENHANCED_VELOCITY=1/' .env.compose
else
  echo 'STRATEGY_ENHANCED_VELOCITY=1' | sudo tee -a .env.compose >/dev/null
fi

# Build from the VM checkout so you do not need a pre-published GHCR tag.
if grep -q '^IMAGE_SOURCE=' .env.compose; then
  sudo sed -i 's/^IMAGE_SOURCE=.*/IMAGE_SOURCE=local_build/' .env.compose
else
  echo 'IMAGE_SOURCE=local_build' | sudo tee -a .env.compose >/dev/null
fi

# Optional but recommended: verify the policy classes before container rebuild.
python3 -m pip install pytest pandas numpy -q
python3 -m pytest strategy_app/engines/test_velocity_policies.py -q

# Rebuild the services touched by the velocity deploy.
sudo docker compose --env-file .env.compose -f docker-compose.yml build strategy_app dashboard
sudo docker compose --env-file .env.compose -f docker-compose.yml --profile ui up -d strategy_app dashboard
```

## Verify

```bash
cd /opt/option_trading

sudo docker compose --env-file .env.compose -f docker-compose.yml --profile ui ps

curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8008}/api/health" | python3 -m json.tool

curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8008}/api/trading/velocity-testing/test" \
  -G \
  --data-urlencode "date_from=2026-04-11" \
  --data-urlencode "date_to=2026-04-18" \
  --data-urlencode "trade_direction=CE" | python3 -m json.tool
```

Dashboard URL:

```text
http://<runtime-vm-external-ip>:8008/trading/velocity-testing
```

## Operator Path

For the normal operator flow, run this from the operator checkout instead of hand-running Compose on the VM:

```bash
cd /path/to/option_trading
bash ./ops/gcp/runtime_lifecycle_interactive.sh
```

Choose `2) Start/restart runtime`. That path publishes `.env.compose` with `ops/gcp/publish_runtime_config.sh` and restarts the runtime VM so the startup script pulls the runtime config bundle and starts Compose.

If you use the operator path with GHCR images, publish a new image tag that contains the velocity changes and set `APP_IMAGE_TAG` to that tag in `.env.compose` before publishing runtime config. If you use `IMAGE_SOURCE=local_build`, the VM builds from its checkout.

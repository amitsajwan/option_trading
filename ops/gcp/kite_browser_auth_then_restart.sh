#!/usr/bin/env bash
# Browser Kite login on the runtime VM (use with SSH port-forward to localhost:5000).
#
# On your laptop (PowerShell), in one terminal:
#   gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b `
#     --project=algo-trading-496203 -- -L 5000:127.0.0.1:5000
#
# In that SSH session on the VM:
#   sudo bash /opt/option_trading/ops/gcp/kite_browser_auth_then_restart.sh
#
# Complete login in your local browser when the script prints the Kite URL.
set -euo pipefail
REPO=/opt/option_trading
cd "$REPO"
PY="$REPO/.venv/bin/python"
set -a
# shellcheck disable=SC1091
source "$REPO/.env.compose"
set +a
export KITE_SKIP_DOTENV_LOAD=1
echo "Starting browser auth (callback http://127.0.0.1:5000/)..."
"$PY" -m ingestion_app.kite_auth --force --credentials-path "$REPO/ingestion_app/credentials.json"
echo "Restarting live data services..."
docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --no-deps --force-recreate ingestion_app snapshot_app persistence_app
sleep 15
bash "$REPO/ops/gcp/_check_live_freshness.sh" || true

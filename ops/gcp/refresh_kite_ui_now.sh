#!/usr/bin/env bash
# Refresh Kite token (headless TOTP) and restart live data path for dashboard UI.
# Run ON the runtime VM as root after creating /opt/option_trading/.env.totp
#
#   sudo bash /opt/option_trading/ops/gcp/refresh_kite_ui_now.sh
#
# See docs/runbooks/LOCAL_LIVE_OPERATIONS.md → "GCP Token Rotation" → T3
set -euo pipefail
REPO=/opt/option_trading
cd "$REPO"
PY="$REPO/.venv/bin/python"
LOG=/tmp/kite_refresh_ui_now.log
exec > >(tee -a "$LOG") 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] refresh_kite_ui_now start"
if [[ ! -f "$REPO/.env.totp" ]]; then
  echo "ERROR: missing $REPO/.env.totp"
  echo "Create it (chmod 600) with KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET, KITE_API_KEY, KITE_API_SECRET"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$REPO/.env.totp"
set +a
"$PY" -m pip install -q pyotp
"$PY" -m ingestion_app.kite_totp_auth --credentials-path "$REPO/ingestion_app/credentials.json"
echo "restarting live data services..."
docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --no-deps --force-recreate ingestion_app snapshot_app persistence_app
sleep 15
bash "$REPO/ops/gcp/_check_live_freshness.sh" || true
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] refresh_kite_ui_now done"

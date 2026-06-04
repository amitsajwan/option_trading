#!/usr/bin/env bash
# Daily Kite access-token refresh — CONTAINER-BASED (no host venv required).
#
# Background: the migrated amit-trading VM has no /opt/option_trading/.venv, so the
# old refresh script (which ran .venv/bin/python) failed silently every morning →
# the token expired → "TokenException: Incorrect api_key or access_token" →
# snapshot_app "no OHLC bars" → blank UI + stalled engine (incident 2026-06-04).
#
# This version runs the headless TOTP auth INSIDE the ingestion_app container
# (which already has kiteconnect + pyotp), then PERSISTS the fresh token to BOTH
# places the runtime reads it:
#   1. host ingestion_app/credentials.json  (docker cp out of the container)
#   2. KITE_ACCESS_TOKEN in .env.compose     (the env the Kite clients consume)
# then recreates the Kite-client containers so they load the new token.
#
# Install: sudo cp ops/gcp/kite_token_refresh.sh /usr/local/bin/kite-token-refresh.sh
#          sudo chmod +x /usr/local/bin/kite-token-refresh.sh
# Invoked by kite-token-refresh.service (timer ~03:00 UTC = 08:30 IST, pre-open).
set -uo pipefail

REPO_ROOT="/opt/option_trading"
LOG_FILE="/var/log/kite-token-refresh.log"
ENV_TOTP="${REPO_ROOT}/.env.totp"
ENV_COMPOSE="${REPO_ROOT}/.env.compose"
CREDS_HOST="${REPO_ROOT}/ingestion_app/credentials.json"
ING="option_trading-ingestion_app-1"
# Kite-dependent containers to recreate so they pick up the new token.
KITE_CLIENTS=("ingestion_app" "snapshot_app")

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "${LOG_FILE}"; }

cd "${REPO_ROOT}" || { log "ERROR: repo root missing"; exit 1; }
log "Kite token refresh starting (containerized)"

if ! docker inspect "${ING}" >/dev/null 2>&1; then
  log "ERROR: ${ING} not running — cannot auth"; exit 1
fi

# 1) Headless TOTP auth inside the ingestion container (creds from .env.totp).
set -a; source "${ENV_TOTP}"; set +a
if ! docker exec \
      -e KITE_USER_ID="${KITE_USER_ID}" -e KITE_PASSWORD="${KITE_PASSWORD}" \
      -e KITE_TOTP_SECRET="${KITE_TOTP_SECRET}" -e KITE_API_KEY="${KITE_API_KEY}" \
      -e KITE_API_SECRET="${KITE_API_SECRET}" \
      "${ING}" \
      python -m ingestion_app.kite_totp_auth \
      --credentials-path /app/ingestion_app/credentials.json >>"${LOG_FILE}" 2>&1; then
  log "ERROR: TOTP auth failed — token NOT refreshed"; exit 1
fi
log "TOTP auth OK"

# 2) Persist fresh credentials.json from container to host.
docker cp "${ING}:/app/ingestion_app/credentials.json" "${CREDS_HOST}" >>"${LOG_FILE}" 2>&1
log "credentials.json copied to host ($(stat -c '%y' "${CREDS_HOST}"))"

# 3) Sync KITE_ACCESS_TOKEN in .env.compose (the env the runtime reads).
NEW=$(python3 -c "import json;print(json.load(open('${CREDS_HOST}'))['access_token'])" 2>/dev/null)
if [ -z "${NEW}" ]; then log "ERROR: could not parse new access_token"; exit 1; fi
cp "${ENV_COMPOSE}" "${ENV_COMPOSE}.bak.tokrefresh.$(date -u +%Y%m%d)" 2>/dev/null || true
if grep -q '^KITE_ACCESS_TOKEN=' "${ENV_COMPOSE}"; then
  sed -i "s|^KITE_ACCESS_TOKEN=.*|KITE_ACCESS_TOKEN=${NEW}|" "${ENV_COMPOSE}"
else
  echo "KITE_ACCESS_TOKEN=${NEW}" >> "${ENV_COMPOSE}"
fi
log "KITE_ACCESS_TOKEN synced in .env.compose (len ${#NEW})"

# 4) Recreate Kite-client containers so they load the new token.
for svc in "${KITE_CLIENTS[@]}"; do
  log "recreating ${svc}"
  docker compose --env-file "${ENV_COMPOSE}" -f docker-compose.yml up -d --no-deps --force-recreate "${svc}" >>"${LOG_FILE}" 2>&1 || log "WARN: recreate ${svc} returned non-zero"
done

# 5) Optional: publish refreshed credentials to the GCS runtime-config bucket.
if [[ -f "ops/gcp/publish_runtime_config.sh" ]] && command -v gcloud &>/dev/null; then
  bash ops/gcp/publish_runtime_config.sh >>"${LOG_FILE}" 2>&1 || log "WARN: GCS publish failed (non-fatal)"
fi

log "Kite token refresh complete"

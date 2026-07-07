#!/usr/bin/env bash
# Daily Dhan token refresh via TOTP — runs at 02:30 UTC (08:00 IST) pre-market,
# and on demand from dhan_token_guard.sh when the live token goes stale.
#
# Hardened (2026-07-07, after the exec-bit incident silently killed the morning
# refresh and the stack ran token-less until 10:52 IST):
#   - invoked via `bash <script>` from systemd — no dependency on the exec bit
#   - flock: refresh and guard never run concurrently
#   - mint retried 3x with backoff (TOTP window roll / transient auth errors)
#   - token VERIFIED against the fundlimit API before containers are recreated
#   - containers recreated only on success; failure exits non-zero so systemd
#     records it and the guard timer retries within 15 minutes
set -uo pipefail
REPO=/opt/option_trading
LOCK=/var/lock/dhan-token.lock
ENVC="$REPO/.env.compose"
log(){ echo "[$(date -u +%FT%TZ)] $*"; }

exec 9>"$LOCK"
flock -w 120 9 || { log "FAILED: could not acquire $LOCK"; exit 1; }

verify_token(){
  local tok
  tok=$(grep '^DHAN_ACCESS_TOKEN=' "$ENVC" | head -1 | cut -d= -f2-)
  [ -n "$tok" ] || return 1
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
    -H "access-token: $tok" -H "Accept: application/json" \
    https://api.dhan.co/v2/fundlimit 2>/dev/null)
  [ "$code" = "200" ]
}

log "dhan TOTP token refresh starting"
minted=0
for attempt in 1 2 3; do
  if python3 "$REPO/ops/gcp/dhan_totp_refresh.py"; then
    if verify_token; then
      log "mint attempt $attempt: token minted and verified against fundlimit API"
      minted=1
      break
    fi
    log "mint attempt $attempt: minted but verification failed — retrying"
  else
    log "mint attempt $attempt: mint failed — retrying"
  fi
  sleep $((attempt * 20))
done

if [ "$minted" != "1" ]; then
  log "FAILED: could not mint a working Dhan token after 3 attempts"
  exit 1
fi

log "recreating ALL Dhan-dependent containers with the fresh token"
cd "$REPO"
docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --no-deps --force-recreate \
  ingestion_app \
  ingestion_app_nifty \
  execution_app \
  execution_app_nifty
log "dhan token refreshed via TOTP + all 4 Dhan containers recreated"

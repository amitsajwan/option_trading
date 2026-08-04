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
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.gcp.yml)
if [ -f docker-compose.seller.yml ]; then
  COMPOSE_FILES+=(-f docker-compose.seller.yml)
fi
# Instrument-GENERIC (2026-08-04): the four Dhan-authenticating service
# FAMILIES (ingestion_app, execution_app, seller_app, depth_collector_dhan)
# each independently authenticate with DHAN_ACCESS_TOKEN at container startup
# -- any instrument's container skipped here keeps trading on its stale token
# until it hits a hard 401. This bug shape hit real money TWICE with a
# hardcoded list: 2026-07-24 (seller_app_nifty missing -- cost two real NIFTY
# seller entries before the fail-latch caught it) and 2026-08-04 (FINNIFTY
# missing entirely -- discovered when snapshot_app_finnifty went unhealthy at
# market open, "no OHLC bars available", because ingestion_app_finnifty was
# still authenticating with yesterday's invalidated token). Derive the list
# from the ACTUALLY RUNNING containers instead of a name list that must be
# remembered per instrument.
RECREATE_SVCS=($(docker ps --format '{{.Names}}' \
  | grep -E '^option_trading-(ingestion_app|execution_app|seller_app|depth_collector_dhan)(_[a-z]+)?-1$' \
  | grep -v historical \
  | sed -E 's/^option_trading-//; s/-1$//' \
  | sort))
if [ "${#RECREATE_SVCS[@]}" -eq 0 ]; then
  log "FAILED: no Dhan-dependent containers found running -- refusing to no-op recreate"
  exit 1
fi
docker compose --env-file .env.compose "${COMPOSE_FILES[@]}" \
  up -d --no-deps --force-recreate "${RECREATE_SVCS[@]}"
log "dhan token refreshed via TOTP + ${#RECREATE_SVCS[@]} Dhan containers recreated (${RECREATE_SVCS[*]})"

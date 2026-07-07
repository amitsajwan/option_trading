#!/usr/bin/env bash
# Dhan token guard — cheap validity probe, self-healing.
#
# Runs every 15 min during market hours (systemd timer dhan-token-guard.timer).
# Probes the fundlimit endpoint with the token currently in .env.compose:
#   200        -> token healthy, exit 0 (no churn)
#   401/403    -> token stale/invalid -> run the full refresh (mint + verify +
#                 recreate the 4 Dhan containers)
#   000/other  -> network blip or Dhan-side error: log and exit 0. Refreshing
#                 on a transient network error would recreate containers
#                 mid-session for nothing; the next probe is 15 min away.
#
# This is the safety net for EVERY failure mode of the 08:00 IST refresh —
# missed timer, script error, VM booted mid-day, token revoked upstream.
set -uo pipefail
REPO=/opt/option_trading
ENVC="$REPO/.env.compose"
log(){ echo "[$(date -u +%FT%TZ)] $*"; }

tok=$(grep '^DHAN_ACCESS_TOKEN=' "$ENVC" | head -1 | cut -d= -f2-)
if [ -z "$tok" ]; then
  log "guard: no DHAN_ACCESS_TOKEN in .env.compose — triggering refresh"
  exec bash "$REPO/ops/gcp/dhan_token_refresh.sh"
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  -H "access-token: $tok" -H "Accept: application/json" \
  https://api.dhan.co/v2/fundlimit 2>/dev/null)

case "$code" in
  200)
    log "guard: token healthy (fundlimit 200)"
    ;;
  401|403)
    log "guard: token STALE (fundlimit $code) — triggering full refresh"
    exec bash "$REPO/ops/gcp/dhan_token_refresh.sh"
    ;;
  *)
    log "guard: inconclusive probe (http $code) — leaving token as-is, next probe in 15m"
    ;;
esac

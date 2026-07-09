#!/usr/bin/env bash
# Dhan token guard — cheap validity probe, self-healing.
#
# Runs every 15 min during market hours (systemd timer dhan-token-guard.timer).
# Probes the fundlimit endpoint with the token currently in .env.compose:
#   200        -> token healthy, exit 0 (no churn), reset failure streak
#   401/403    -> token stale/invalid -> refresh immediately (unambiguous)
#   other      -> track a failure streak; 2 consecutive non-200 probes
#                 (~15-30 min sustained) triggers a refresh regardless of the
#                 exact code. A single blip stays silent (avoids mid-session
#                 container churn for nothing); the SAME problem persisting
#                 across two probes is never just a network blip.
#
# Hardened 2026-07-09: Dhan returned HTTP 400 (not 401/403) for an expired
# token after the 08:00 refresh crashed — the old 401/403-only logic called
# this "inconclusive" for 1h+ straight during live trading and never
# escalated. The mint script itself is also hardened separately (surrogate-
# escape file read) so the root cause can't recur, but this streak-based
# fallback is the safety net for ANY other non-200 pattern we haven't seen yet.
#
# This is the safety net for EVERY failure mode of the 08:00 IST refresh —
# missed timer, script error, VM booted mid-day, token revoked upstream.
set -uo pipefail
REPO=/opt/option_trading
ENVC="$REPO/.env.compose"
STATE="/opt/option_trading/.run/dhan_guard_fail_streak"
log(){ echo "[$(date -u +%FT%TZ)] $*"; }

mkdir -p "$(dirname "$STATE")"
streak=$(cat "$STATE" 2>/dev/null || echo 0)

tok=$(grep '^DHAN_ACCESS_TOKEN=' "$ENVC" | head -1 | cut -d= -f2-)
if [ -z "$tok" ]; then
  log "guard: no DHAN_ACCESS_TOKEN in .env.compose — triggering refresh"
  echo 0 > "$STATE"
  exec bash "$REPO/ops/gcp/dhan_token_refresh.sh"
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  -H "access-token: $tok" -H "Accept: application/json" \
  https://api.dhan.co/v2/fundlimit 2>/dev/null)

case "$code" in
  200)
    log "guard: token healthy (fundlimit 200)"
    echo 0 > "$STATE"
    ;;
  401|403)
    log "guard: token STALE (fundlimit $code) — triggering full refresh"
    echo 0 > "$STATE"
    exec bash "$REPO/ops/gcp/dhan_token_refresh.sh"
    ;;
  *)
    streak=$((streak + 1))
    if [ "$streak" -ge 2 ]; then
      log "guard: $streak consecutive non-200 probes (latest http $code) — sustained failure, triggering refresh"
      echo 0 > "$STATE"
      exec bash "$REPO/ops/gcp/dhan_token_refresh.sh"
    fi
    log "guard: inconclusive probe (http $code), streak=$streak — leaving token as-is, next probe in 15m"
    echo "$streak" > "$STATE"
    ;;
esac

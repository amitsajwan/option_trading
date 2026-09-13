#!/usr/bin/env bash
# Silent-consumer liveness probe (2026-07-17, extended 2026-09-13). Twice on
# 2026-07-16 the buyer strategies went silent mid-session on a stale Redis
# consumer lock: zero error logs, zero new decision traces, snapshots flowing
# fine upstream. This probe alerts on the one signal that caught that failure
# mode: no fresh decision trace during market hours.
#
# 2026-09-13: extended with an always-on container-down check after finding
# two real-money seller containers had been dead for 5 days with zero alerts
# (see the check_container_up comment below) -- the original version only
# ever looked at buyer (strategy_app*) containers that were already running.
#
# Install: ops/gcp/install_liveness_units.sh (systemd timer, every 5 min).
# Test:    bash check_strategy_liveness.sh --force
set -u
REPO=/opt/option_trading
MAX_AGE_MIN=6
FORCE=${1:-}

TOKEN=$(sudo grep -E '^ALERT_TELEGRAM_TOKEN=' "$REPO/.env.compose" | cut -d= -f2-)
CHAT=$(sudo grep -E '^ALERT_TELEGRAM_CHAT_ID=' "$REPO/.env.compose" | cut -d= -f2-)

alert() {
  local msg="$1"
  echo "$(date -u +%FT%TZ) ALERT: $msg"
  if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    curl -sf "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      -d chat_id="${CHAT}" -d text="⚠️ ${msg}" >/dev/null || true
  fi
}

# Container-down check (2026-09-13): runs EVERY invocation, NOT gated to
# market hours -- a real-money process dying at 15:50 IST shouldn't wait for
# the next market-hours window to be noticed, and a seller can be mid-position
# outside the buyer's 09:20-15:30 window too. Found seller_app (BankNifty) and
# seller_app_nifty -- both REAL-MONEY live sellers -- had been SIGKILLed
# (exit 137, RestartCount=0, consistent with an explicit stop/kill rather than
# a crash the restart policy would have healed) 5 DAYS earlier with zero
# alerting, because this probe only ever checked strategy_app* (buyer)
# containers, and only ones already `docker ps`-running -- a fully stopped
# container was invisible on both counts. Compares docker-compose's own
# declared service list (deployment intent) against actual container state,
# so any future instrument/service is covered automatically, same
# instrument-generic principle as the buyer loop below.
check_container_up() { # $1 = compose service name (e.g. seller_app_nifty)
  local svc="$1" cname status
  cname="option_trading-${svc}-1"
  status=$(sudo docker inspect --format '{{.State.Status}}' "$cname" 2>/dev/null)
  if [ -z "$status" ]; then
    return  # not deployed at all (e.g. seller_app_midcpnifty, cold-start) -- not an error
  fi
  if [ "$status" != "running" ]; then
    alert "${cname}: container is '${status}', not running -- real-money process is DOWN. Check: sudo docker logs ${cname} --tail 50"
  fi
}

for svc in $(cd "$REPO" && sudo docker compose --env-file .env.compose \
               -f docker-compose.yml -f docker-compose.gcp.yml -f docker-compose.seller.yml \
               config --services 2>/dev/null \
               | grep -E '^(strategy_app|seller_app)' | grep -v historical | sort); do
  check_container_up "$svc"
done

# Market hours gate (IST): Mon-Fri 09:20-15:30, unless --force. Only the
# decision-trace staleness check below needs this (no new buyer decisions are
# expected outside market hours anyway) -- the container-down check above
# always runs.
now_ist=$(TZ=Asia/Kolkata date '+%u %H%M')
dow=${now_ist% *}; hhmm=${now_ist#* }
if [ "$FORCE" != "--force" ]; then
  [ "$dow" -gt 5 ] && exit 0
  [ "$hhmm" -lt 0920 ] && exit 0
  [ "$hhmm" -gt 1530 ] && exit 0
fi

check() { # $1 = label, $2 = trace collection
  local label="$1" coll="$2"
  local last
  last=$(sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --eval "
    var d = db.${coll}.find().sort({\$natural:-1}).limit(1).toArray()[0];
    print(d ? d.received_at_ist : 'none');
  " 2>/dev/null | tail -1)
  if [ "$last" = "none" ] || [ -z "$last" ]; then
    alert "${label}: no decision traces found at all"
    return
  fi
  # age in minutes via python (IST offset-aware string)
  local age
  age=$(python3 - "$last" <<'PY'
import sys
from datetime import datetime, timezone, timedelta
ist = timezone(timedelta(hours=5, minutes=30))
try:
    t = datetime.fromisoformat(sys.argv[1])
    if t.tzinfo is None:
        t = t.replace(tzinfo=ist)
    print(int((datetime.now(ist) - t).total_seconds() // 60))
except Exception:
    print(9999)
PY
)
  if [ "$age" -ge "$MAX_AGE_MIN" ]; then
    alert "${label}: last decision trace is ${age} min old (market hours) — consumer likely stuck; try: docker restart the strategy container"
  else
    echo "$(date -u +%FT%TZ) OK ${label}: last trace ${age} min old"
  fi
}

# Instrument-GENERIC (2026-08-04): derive the instrument set from the running
# strategy containers, and each one's trace collection from the container-name
# suffix (same "{base}" primary / "{base}_{slug}" secondary rule as
# contracts_app.sim_namespace). The old hardcoded two-check list silently left
# FINNIFTY's buyer with zero liveness coverage.
for c in $(sudo docker ps --format '{{.Names}}' \
             | grep -E '^option_trading-strategy_app(_[a-z]+)?-1$' \
             | grep -v historical | sort); do
  suffix=$(echo "$c" | sed -E 's/^option_trading-strategy_app(_[a-z]+)?-1$/\1/')
  if [ -z "$suffix" ]; then
    check "BANKNIFTY buyer (strategy_app)" "strategy_decision_traces"
  else
    inst=$(echo "${suffix#_}" | tr '[:lower:]' '[:upper:]')
    check "${inst} buyer (strategy_app${suffix})" "strategy_decision_traces${suffix}"
  fi
done

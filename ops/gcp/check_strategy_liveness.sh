#!/usr/bin/env bash
# Silent-consumer liveness probe (2026-07-17). Twice on 2026-07-16 the buyer
# strategies went silent mid-session on a stale Redis consumer lock: zero error
# logs, zero new decision traces, snapshots flowing fine upstream. This probe
# alerts on the one signal that catches that failure mode: no fresh decision
# trace during market hours.
#
# Install: ops/gcp/install_liveness_units.sh (systemd timer, every 5 min).
# Test:    bash check_strategy_liveness.sh --force
set -u
REPO=/opt/option_trading
MAX_AGE_MIN=6
FORCE=${1:-}

# Market hours gate (IST): Mon-Fri 09:20-15:30, unless --force
now_ist=$(TZ=Asia/Kolkata date '+%u %H%M')
dow=${now_ist% *}; hhmm=${now_ist#* }
if [ "$FORCE" != "--force" ]; then
  [ "$dow" -gt 5 ] && exit 0
  [ "$hhmm" -lt 0920 ] && exit 0
  [ "$hhmm" -gt 1530 ] && exit 0
fi

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

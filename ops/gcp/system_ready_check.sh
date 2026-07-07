#!/usr/bin/env bash
# 09:00 IST — "SYSTEM READY" verdict to Telegram before market open.
# Combines: config contract + container health + token validity.
REPO=/opt/option_trading
TG="bash $REPO/ops/gcp/tg_send.sh"
log(){ echo "[$(date -u +%FT%TZ)] $*"; }

contract=$(python3 "$REPO/ops/check_config_contract.py" --quiet 2>&1 | head -1); crc=$?
unhealthy=$(docker ps --format '{{.Names}} {{.Status}}' | grep unhealthy | awk '{print $1}' | paste -sd, -)
total=$(docker ps -q | wc -l)

tok=$(grep '^DHAN_ACCESS_TOKEN=' "$REPO/.env.compose" | head -1 | cut -d= -f2-)
tcode=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  -H "access-token: $tok" -H "Accept: application/json" \
  https://api.dhan.co/v2/fundlimit 2>/dev/null)

if [ "$crc" = "0" ] && [ -z "$unhealthy" ] && [ "$tcode" = "200" ]; then
  msg="✅ SYSTEM READY $(TZ=Asia/Kolkata date +%H:%M) IST — ${total} containers up, config contract PASS, Dhan token OK"
else
  msg="🚨 SYSTEM NOT READY $(TZ=Asia/Kolkata date +%H:%M) IST"
  [ "$crc" != "0" ] && msg="$msg | $contract"
  [ -n "$unhealthy" ] && msg="$msg | unhealthy: $unhealthy"
  [ "$tcode" != "200" ] && msg="$msg | Dhan token probe: HTTP $tcode"
fi
log "$msg"
$TG "$msg"

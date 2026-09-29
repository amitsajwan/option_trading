#!/usr/bin/env bash
# vm_lifecycle.sh -- park and un-park the live stack on the runtime VM.
#
#   sudo bash ops/vm_lifecycle.sh freeze   # halt all trading, stop timers + containers
#   sudo bash ops/vm_lifecycle.sh thaw     # bring back exactly what freeze stopped
#   sudo bash ops/vm_lifecycle.sh status
#
# Runs ON the VM. From a laptop use ops/shutdown.sh / ops/resume.sh, which also
# stop/start the VM itself. Full procedure: docs/RESUME.md.
#
# freeze records a manifest (the running containers + our enabled systemd
# timers) under .run/lifecycle/, so thaw restores the stack as it actually was.
# It does not rely on a hardcoded service list: containers deliberately left
# stopped (e.g. the BankNifty/NIFTY real-money sellers, down since 2026-09-08)
# stay stopped. Containers are `docker stop`ped, not removed, so
# `restart: unless-stopped` keeps them down across a VM reboot.
#
# thaw runs ensure_safe_defaults.sh BEFORE starting anything, so every buyer and
# seller comes back HALTED. Resuming real trading stays a separate, deliberate
# human step. If broker credentials are missing or rejected, thaw stops the
# broker-connected containers and leaves the timers off, instead of letting
# them crash-loop and fire alerts.
set -uo pipefail
REPO=/opt/option_trading
STATE="$REPO/.run/lifecycle"
COMPOSE="docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml -f docker-compose.seller.yml"
# Service families that authenticate with the broker at startup (the same set
# dhan_token_refresh.sh recreates).
BROKER_RE='^option_trading-(ingestion_app|execution_app|seller_app|depth_collector_dhan)'
INFRA_RE='^option_trading-(mongo|redis)-'
log(){ echo "[lifecycle $(date -u +%H:%M:%SZ)] $*"; }
die(){ log "FAILED: $*"; exit 1; }

[ "$(id -u)" = "0" ] || die "run with sudo"
cd "$REPO" || die "repo missing at $REPO"

running_ours(){ docker ps --format '{{.Names}}' | grep '^option_trading-' | sort; }

custom_enabled_timers(){
  local f n
  for f in /etc/systemd/system/*.timer; do
    [ -e "$f" ] || continue
    n=$(basename "$f")
    systemctl is-enabled --quiet "$n" 2>/dev/null && echo "$n"
  done
}

freeze(){
  mkdir -p "$STATE"
  local running; running=$(running_ours)
  if [ -z "$running" ] && [ -s "$STATE/containers.txt" ]; then
    # Already frozen: re-recording now would overwrite the manifest with an
    # empty list and make thaw a no-op.
    log "nothing running and a manifest exists -- already frozen, keeping manifest"
  else
    echo "$running" > "$STATE/containers.txt"
    custom_enabled_timers > "$STATE/timers.txt"
    git rev-parse HEAD > "$STATE/commit" 2>/dev/null || true
    date -u +%FT%TZ > "$STATE/frozen_at"
    log "manifest: $(wc -l < "$STATE/containers.txt") containers, $(wc -l < "$STATE/timers.txt") timers -> $STATE"
  fi

  log "halting every buyer and seller"
  bash "$REPO/ops/gcp/ensure_safe_defaults.sh" | sed 's/^/    /'

  if [ -s "$STATE/timers.txt" ]; then
    log "disabling timers: $(tr '\n' ' ' < "$STATE/timers.txt")"
    xargs -r systemctl disable --now < "$STATE/timers.txt"
  fi

  running=$(running_ours)
  if [ -n "$running" ]; then
    local apps infra
    apps=$(echo "$running" | grep -vE "$INFRA_RE" || true)
    infra=$(echo "$running" | grep -E "$INFRA_RE" || true)
    log "stopping $(echo "$apps" | grep -c . ) app containers, then mongo/redis last"
    [ -n "$apps" ] && echo "$apps" | xargs -r docker stop -t 30 >/dev/null
    [ -n "$infra" ] && echo "$infra" | xargs -r docker stop -t 60 >/dev/null
  fi
  log "FROZEN. $(running_ours | grep -c .) containers still running."
}

thaw(){
  [ -s "$STATE/containers.txt" ] || die "no manifest at $STATE/containers.txt -- nothing to thaw (was freeze run?)"

  log "halting every buyer and seller BEFORE anything starts"
  bash "$REPO/ops/gcp/ensure_safe_defaults.sh" | sed 's/^/    /'

  local infra apps
  infra=$(grep -E "$INFRA_RE" "$STATE/containers.txt" || true)
  apps=$(grep -vE "$INFRA_RE" "$STATE/containers.txt" || true)
  [ -n "$infra" ] && { log "starting mongo/redis"; echo "$infra" | xargs -r docker start >/dev/null; sleep 10; }
  log "starting $(echo "$apps" | grep -c .) app containers"
  [ -n "$apps" ] && echo "$apps" | xargs -r docker start >/dev/null
  sleep 20

  local broker_ok=0
  if [ -s "$REPO/.env.totp" ]; then
    log "minting a fresh broker token from .env.totp (also recreates broker-connected containers)"
    local out rc
    out=$(bash "$REPO/ops/gcp/dhan_token_refresh.sh" 2>&1); rc=$?
    echo "$out" | sed 's/^/    /'
    [ "$rc" = 0 ] && broker_ok=1
  else
    log "no .env.totp on the VM -- cannot authenticate with the broker"
  fi

  if [ "$broker_ok" = 1 ]; then
    if [ -s "$STATE/timers.txt" ]; then
      log "re-enabling timers: $(tr '\n' ' ' < "$STATE/timers.txt")"
      xargs -r systemctl enable --now < "$STATE/timers.txt"
    fi
  else
    log "BROKER NOT AUTHENTICATED -- stopping broker-connected containers and leaving timers OFF"
    log "fix: put valid credentials in $REPO/.env.totp (or: bash ops/resume.sh --totp <file>) and thaw again"
    running_ours | grep -E "$BROKER_RE" | xargs -r docker stop -t 30 >/dev/null
  fi

  sleep 15
  status
  if [ "$broker_ok" = 1 ]; then
    log "config contract:"
    python3 "$REPO/ops/check_config_contract.py" --quiet 2>&1 | sed 's/^/    /' || true
    log "THAWED. Every instrument is HALTED -- remove operator_halt deliberately to trade (docs/RESUME.md)."
  else
    log "THAWED WITHOUT BROKER (data/dashboard up, trading and ingestion down)."
    exit 2
  fi
}

status(){
  local expected running
  expected=$( [ -s "$STATE/containers.txt" ] && wc -l < "$STATE/containers.txt" || echo "?")
  running=$(running_ours | grep -c .)
  log "containers running: $running (manifest: $expected)$( [ -s "$STATE/frozen_at" ] && echo ", last frozen $(cat "$STATE/frozen_at")")"
  local unhealthy; unhealthy=$(docker ps --format '{{.Names}} {{.Status}}' | grep -c unhealthy || true)
  [ "$unhealthy" != "0" ] && docker ps --format '    {{.Names}} {{.Status}}' | grep unhealthy
  log "timers enabled: $(custom_enabled_timers | tr '\n' ' ')"
  local s halted=0 total=0
  for s in strategy_app strategy_app_nifty strategy_app_sensex strategy_app_midcpnifty strategy_app_finnifty; do
    total=$((total+1)); [ -f "$REPO/.run/$s/operator_halt" ] && halted=$((halted+1))
  done
  log "buyer halts: $halted/$total present"
}

case "${1:-}" in
  freeze) freeze ;;
  thaw)   thaw ;;
  status) status ;;
  *) echo "usage: sudo bash ops/vm_lifecycle.sh {freeze|thaw|status}"; exit 1 ;;
esac

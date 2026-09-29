#!/usr/bin/env bash
# resume.sh -- ONE-CLICK resume of the parked trading stack. Runs from your laptop.
#
#   bash ops/resume.sh                          # same broker account as before
#   bash ops/resume.sh --totp path/to/.env.totp # new broker account (see docs/RESUME.md)
#
# Needs: gcloud installed and logged in (gcloud auth login), Git Bash on Windows.
# Starts the VM (disk and reserved IP were kept by ops/shutdown.sh), optionally
# uploads new broker credentials, then runs `ops/vm_lifecycle.sh thaw` on the
# VM: every instrument comes back HALTED, services are restarted exactly as
# they were, a fresh broker token is minted, timers are re-enabled, and health
# is checked. Nothing trades until you remove an operator_halt yourself.
set -euo pipefail
PROJECT="${PROJECT:-trader-502012}"
ZONE="${ZONE:-asia-south1-b}"
VM="${VM:-trader-runtime-01}"
CONF="${GCLOUD_CONFIG:-trader-new}"
TOTP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --totp) TOTP="${2:?--totp needs a file path}"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1 (see --help)"; exit 1 ;;
  esac
done
g(){ gcloud --project="$PROJECT" --configuration="$CONF" "$@"; }
ssh_vm(){ g compute ssh "$VM" --zone="$ZONE" --command="$1"; }
log(){ echo "[resume $(date +%H:%M:%S)] $*"; }

if [ -n "$TOTP" ]; then
  [ -f "$TOTP" ] || { echo "no such file: $TOTP"; exit 1; }
  for k in DHAN_CLIENT_ID DHAN_PIN DHAN_TOTP_SECRET; do
    grep -q "^$k=." "$TOTP" || { echo "$TOTP is missing $k (need all of DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET)"; exit 1; }
  done
fi

status=$(g compute instances describe "$VM" --zone="$ZONE" --format='value(status)')
log "VM $VM is $status"
if [ "$status" != "RUNNING" ]; then
  log "starting VM"
  g compute instances start "$VM" --zone="$ZONE" --quiet
fi

log "waiting for SSH"
for i in $(seq 1 30); do
  if ssh_vm "true" >/dev/null 2>&1; then break; fi
  [ "$i" = 30 ] && { log "VM not reachable over SSH after ~5 min"; exit 1; }
  sleep 10
done

if [ -n "$TOTP" ]; then
  log "uploading new broker credentials"
  g compute scp "$TOTP" "$VM:/tmp/.env.totp.new" --zone="$ZONE"
  ssh_vm "sudo install -m 600 -o \$(stat -c %U /opt/option_trading/.env.compose) /tmp/.env.totp.new /opt/option_trading/.env.totp && rm -f /tmp/.env.totp.new"
fi

log "thawing the stack on the VM"
rc=0
ssh_vm "cd /opt/option_trading && sudo bash ops/vm_lifecycle.sh thaw" || rc=$?

ip=$(g compute instances describe "$VM" --zone="$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo
if [ "$rc" = 0 ]; then
  log "DONE. Dashboard: http://$ip:8008   (every instrument is HALTED until you un-halt it)"
elif [ "$rc" = 2 ]; then
  log "VM and data are up, but the BROKER DID NOT AUTHENTICATE -- trading/ingestion left off."
  log "Re-run with valid credentials:  bash ops/resume.sh --totp path/to/.env.totp"
  exit 2
else
  log "thaw failed (exit $rc) -- see output above; docs/RESUME.md has manual steps"
  exit "$rc"
fi

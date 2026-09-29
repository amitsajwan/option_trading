#!/usr/bin/env bash
# shutdown.sh -- park the whole trading stack cheaply. Runs from your laptop.
#
#   bash ops/shutdown.sh              # freeze services, then STOP the VM
#   bash ops/shutdown.sh --keep-vm    # freeze services only, VM keeps running
#
# Runs `ops/vm_lifecycle.sh freeze` on the VM: every buyer and seller is halted,
# the manifest of running services and timers is recorded, and timers and
# containers are stopped. Then the VM is stopped, not deleted. The disk
# (code, Mongo, data, credentials) and the reserved static IP survive, so
# `bash ops/resume.sh` brings everything back. A stopped VM bills only for
# its disk and the reserved IP.
set -euo pipefail
PROJECT="${PROJECT:-trader-502012}"
ZONE="${ZONE:-asia-south1-b}"
VM="${VM:-trader-runtime-01}"
CONF="${GCLOUD_CONFIG:-trader-new}"
KEEP_VM=0
[ "${1:-}" = "--keep-vm" ] && KEEP_VM=1
g(){ gcloud --project="$PROJECT" --configuration="$CONF" "$@"; }
log(){ echo "[shutdown $(date +%H:%M:%S)] $*"; }

log "freezing the stack on the VM"
g compute ssh "$VM" --zone="$ZONE" --command="cd /opt/option_trading && sudo bash ops/vm_lifecycle.sh freeze"

if [ "$KEEP_VM" = 1 ]; then
  log "done (VM left running)"
else
  log "stopping VM $VM"
  g compute instances stop "$VM" --zone="$ZONE" --quiet
  log "DONE. VM stopped; disk and reserved IP kept. Bring it back with: bash ops/resume.sh"
fi

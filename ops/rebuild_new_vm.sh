#!/usr/bin/env bash
# rebuild_new_vm.sh -- ONE COMMAND to recreate the whole stack on a brand-new
# GCP VM from the laptop backup. Runs from your laptop (Git Bash), repo root.
#
#   bash ops/rebuild_new_vm.sh --project <gcp-project> --backup <dir> [--totp new.env.totp]
#
# Options (defaults in brackets):
#   --project P   GCP project to build in (required; billing must be enabled)
#   --backup D    local backup dir [C:/code/option_trading/gcp_backup_2026-09-29]
#   --totp F      NEW broker credentials (3 lines, see docs/RESUME.md). Without
#                 it the backed-up (dead) ones are used and the broker stays off.
#   --zone Z      [asia-south1-b]      --vm NAME        [trader-runtime-01]
#   --machine T   [e2-standard-4]      --disk GB        [80]
#   --no-static-ip   use an ephemeral IP (tests); default reserves "<vm>-ip"
#
# What it does: enables Compute, creates the dashboard firewall rule, reserves a
# static IP, creates an Ubuntu 22.04 VM, uploads the backup (~5 GB -- the slow
# part), then runs ops/restore_from_backup.sh on the VM, which restores code,
# Mongo, data, models and state, HALTS every instrument, builds and starts the
# services, and mints a broker token. Nothing trades until you un-halt.
# Afterwards, park/resume work as usual: ops/shutdown.sh / ops/resume.sh
# (pass the same PROJECT/ZONE/VM env vars if you changed them).
set -euo pipefail
PROJECT=""; BACKUP="C:/code/option_trading/gcp_backup_2026-09-29"; TOTP=""
ZONE="asia-south1-b"; VM="trader-runtime-01"; MACHINE="e2-standard-4"; DISK=80; STATIC=1
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --backup)  BACKUP="$2"; shift 2 ;;
    --totp)    TOTP="$2"; shift 2 ;;
    --zone)    ZONE="$2"; shift 2 ;;
    --vm)      VM="$2"; shift 2 ;;
    --machine) MACHINE="$2"; shift 2 ;;
    --disk)    DISK="$2"; shift 2 ;;
    --no-static-ip) STATIC=0; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1 (see --help)"; exit 1 ;;
  esac
done
[ -n "$PROJECT" ] || { echo "--project is required (see --help)"; exit 1; }
REGION="${ZONE%-*}"
g(){ gcloud --project="$PROJECT" "$@"; }
log(){ echo "[rebuild $(date +%H:%M:%S)] $*"; }

log "checking the backup"
for f in SHA256SUMS COMMIT mongo_trading_ai.archive.gz secrets.tar.gz run.tar.gz models.tar.gz parquet.tar; do
  [ -e "$BACKUP/$f" ] || { echo "backup is missing $f: $BACKUP"; exit 1; }
done
(cd "$BACKUP" && sha256sum -c --quiet SHA256SUMS) || { echo "backup checksums FAILED"; exit 1; }
if [ -n "$TOTP" ]; then
  for k in DHAN_CLIENT_ID DHAN_PIN DHAN_TOTP_SECRET; do
    grep -q "^$k=." "$TOTP" || { echo "$TOTP is missing $k"; exit 1; }
  done
fi

log "enabling Compute Engine API (first time can take a minute)"
g services enable compute.googleapis.com

if ! g compute firewall-rules describe allow-dashboard >/dev/null 2>&1; then
  log "creating firewall rule allow-dashboard (tcp:8008)"
  g compute firewall-rules create allow-dashboard --allow=tcp:8008 --source-ranges=0.0.0.0/0 --quiet
fi

ADDR_FLAG=()
if [ "$STATIC" = 1 ]; then
  if ! g compute addresses describe "$VM-ip" --region="$REGION" >/dev/null 2>&1; then
    log "reserving static IP $VM-ip"
    g compute addresses create "$VM-ip" --region="$REGION" --quiet
  fi
  ADDR_FLAG=(--address="$(g compute addresses describe "$VM-ip" --region="$REGION" --format='value(address)')")
fi

if g compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1; then
  log "VM $VM already exists -- reusing it"
  [ "$(g compute instances describe "$VM" --zone="$ZONE" --format='value(status)')" = RUNNING ] \
    || g compute instances start "$VM" --zone="$ZONE" --quiet
else
  log "creating VM $VM ($MACHINE, ${DISK} GB, Ubuntu 22.04)"
  g compute instances create "$VM" --zone="$ZONE" --machine-type="$MACHINE" \
    --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
    --boot-disk-size="${DISK}GB" --boot-disk-type=pd-balanced "${ADDR_FLAG[@]}" --quiet
fi

log "waiting for SSH"
for i in $(seq 1 30); do
  g compute ssh "$VM" --zone="$ZONE" --command=true >/dev/null 2>&1 && break
  [ "$i" = 30 ] && { log "VM not reachable over SSH"; exit 1; }
  sleep 10
done

log "uploading the backup (~5 GB; this is the slow step)"
g compute ssh "$VM" --zone="$ZONE" --command="mkdir -p ~/backup"
g compute scp --recurse "$BACKUP"/* "$VM:~/backup/" --zone="$ZONE"
g compute scp ops/restore_from_backup.sh "$VM:~/restore_from_backup.sh" --zone="$ZONE"
TOTP_ARG=""
if [ -n "$TOTP" ]; then
  g compute scp "$TOTP" "$VM:~/new.env.totp" --zone="$ZONE"
  TOTP_ARG="\$HOME/new.env.totp"
fi

log "restoring on the VM (docker install, mongorestore, image builds: ~30-60 min)"
rc=0
g compute ssh "$VM" --zone="$ZONE" --command="sudo bash ~/restore_from_backup.sh ~/backup $TOTP_ARG" || rc=$?
g compute ssh "$VM" --zone="$ZONE" --command="rm -f ~/new.env.totp" || true

ip=$(g compute instances describe "$VM" --zone="$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo
if [ "$rc" = 0 ]; then
  log "DONE. Dashboard: http://$ip:8008   Every instrument is HALTED."
  log "Whitelist $ip with the broker. The backup copy stays on the VM in ~/backup (delete it when happy)."
else
  log "restore exited $rc -- see output above; docs/RESUME.md has the manual steps"
  exit "$rc"
fi

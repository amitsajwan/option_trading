#!/usr/bin/env bash
# restore_from_backup.sh -- rebuild the stack on a FRESH Ubuntu VM from a backup.
# Disaster path only: use it when the original VM or GCP project is gone. If
# the VM still exists, `bash ops/resume.sh` from your laptop is the path.
#
#   sudo bash restore_from_backup.sh /path/to/backup_dir [new.env.totp]
#
# new.env.totp (optional): credentials for a NEW broker account; replaces the
# backed-up (dead) one before the token is minted. REF=<git ref> overrides the
# code version (default: the backup's COMMIT file).
#
# The backup dir is what the 2026-09-29 closure produced:
#   mongo_trading_ai.archive.gz  parquet.tar  run.tar.gz  models.tar.gz
#   secrets.tar.gz  vol_option_trading_*.tar.gz  SHA256SUMS  COMMIT  lifecycle/
# See docs/RESUME.md ("Path B"). Status: every step is scripted and the
# backup's integrity was verified, but this full run has never been exercised
# end-to-end on a fresh VM. Watch the first run.
set -euo pipefail
BK="${1:?usage: sudo bash restore_from_backup.sh /path/to/backup_dir}"
BK="$(cd "$BK" && pwd)"
NEW_TOTP="${2:-}"
[ -z "$NEW_TOTP" ] || NEW_TOTP="$(cd "$(dirname "$NEW_TOTP")" && pwd)/$(basename "$NEW_TOTP")"
REPO=/opt/option_trading
CLONE_URL="${CLONE_URL:-https://github.com/amitsajwan/option_trading.git}"
COMPOSE="docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml -f docker-compose.seller.yml"
log(){ echo "[restore $(date -u +%H:%M:%SZ)] $*"; }
[ "$(id -u)" = "0" ] || { echo "run with sudo"; exit 1; }

log "1/9 verifying backup checksums"
(cd "$BK" && sha256sum -c SHA256SUMS)

log "2/9 installing docker (if missing)"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

log "3/9 cloning repo at the backed-up commit"
if [ ! -d "$REPO/.git" ]; then
  git clone "$CLONE_URL" "$REPO"
fi
cd "$REPO"
git fetch --all --quiet
git checkout "${REF:-$(cat "$BK/COMMIT")}"
OWNER="${SUDO_USER:-root}"

log "4/9 restoring secrets, models, run state, parquet data"
tar -xzf "$BK/secrets.tar.gz" -C "$REPO"
if [ -n "$NEW_TOTP" ]; then
  install -m 600 "$NEW_TOTP" "$REPO/.env.totp"
  log "    installed NEW broker credentials from $NEW_TOTP"
fi
chmod 600 "$REPO/.env.compose" "$REPO/.env.totp"
tar -xzf "$BK/models.tar.gz" -C "$REPO"
tar -xzf "$BK/run.tar.gz" -C "$REPO"
if [ -d "$BK/lifecycle" ]; then  # service/timer manifest, so ops/resume.sh (thaw) works later
  mkdir -p "$REPO/.run/lifecycle" && cp "$BK"/lifecycle/*.txt "$REPO/.run/lifecycle/"
fi
mkdir -p "$REPO/.data"
tar -xf "$BK/parquet.tar" -C "$REPO/.data"
chown -R "$OWNER" "$REPO"

log "5/9 restoring docker named volumes (seller state incl. operator_halt)"
for f in "$BK"/vol_*.tar.gz; do
  [ -e "$f" ] || continue
  vol=$(basename "$f" .tar.gz); vol=${vol#vol_}
  docker volume create "$vol" >/dev/null
  docker run --rm -v "$vol:/v" -v "$BK:/in:ro" alpine tar -xzf "/in/$(basename "$f")" -C /v
  log "    $vol"
done

log "6/9 starting mongo alone and restoring the database"
$COMPOSE up -d mongo
for i in $(seq 1 30); do
  docker exec option_trading-mongo-1 mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && break
  sleep 5
done
docker cp "$BK/mongo_trading_ai.archive.gz" option_trading-mongo-1:/tmp/restore.archive.gz
docker exec option_trading-mongo-1 mongorestore --archive=/tmp/restore.archive.gz --gzip --drop
docker exec option_trading-mongo-1 rm -f /tmp/restore.archive.gz

log "7/9 halting every buyer and seller BEFORE any trading service starts"
bash "$REPO/ops/gcp/ensure_safe_defaults.sh"

log "8/9 building images and starting the services that were running at shutdown"
services=""
if [ -s "$BK/lifecycle/containers.txt" ]; then
  services=$(sed -E 's/^option_trading-(.*)-1$/\1/' "$BK/lifecycle/containers.txt" | tr '\n' ' ')
fi
if [ -z "$services" ]; then
  log "    no manifest in backup -- bringing up the full default set"
fi
# Build EVERY image, not just $services: suffixed services (seller_app_sensex,
# execution_app_nifty, ...) have no build: of their own and reuse the image of
# their base service, which may itself be absent from the manifest (the
# BankNifty seller was stopped at closure). Building never starts a container.
$COMPOSE build
# shellcheck disable=SC2086
$COMPOSE up -d --no-deps $services

log "9/9 minting a broker token and installing the scheduled jobs"
BROKER_OK=0
bash "$REPO/ops/gcp/dhan_token_refresh.sh" && BROKER_OK=1
if [ "$BROKER_OK" = 1 ]; then
  bash "$REPO/ops/gcp/install_dhan_token_units.sh"
  bash "$REPO/ops/gcp/install_liveness_units.sh"
else
  log "    BROKER DID NOT AUTHENTICATE -- stopping broker-connected containers, no timers installed"
  docker ps --format '{{.Names}}' | grep -E '^option_trading-(ingestion_app|execution_app|seller_app|depth_collector_dhan)'     | xargs -r docker stop -t 30 >/dev/null
fi

log "status:"
bash "$REPO/ops/vm_lifecycle.sh" status 2>&1 | sed 's/^/    /' || true
if [ "$BROKER_OK" = 1 ]; then
  python3 "$REPO/ops/check_config_contract.py" --quiet 2>&1 | sed 's/^/    /' || true
  log "DONE. Every instrument is HALTED. This VM has a NEW public IP: whitelist it with the broker."
else
  log "RESTORED WITHOUT BROKER. Put valid credentials in $REPO/.env.totp, then:"
  log "  sudo bash $REPO/ops/gcp/dhan_token_refresh.sh && sudo bash $REPO/ops/gcp/install_dhan_token_units.sh && sudo bash $REPO/ops/gcp/install_liveness_units.sh"
  log "  (or from the laptop: bash ops/resume.sh --totp <file>, after a freeze)"
  exit 2
fi

#!/usr/bin/env bash
# ensure_safe_defaults.sh — the mandatory first step of ANY deploy or restore.
#
# Found 2026-09-16 while auditing disaster-recovery restore safety: a fresh
# deploy (or a restore onto a new VM after GCP account expiry/loss) brings up
# every container reading straight from .env.compose, with NO guarantee that
# real-money execution is off by default. Two concrete gaps this closes:
#
#   1. operator_halt files live under the gitignored .run/ mount -- they are
#      NOT part of any backup (git, mongodump, or the .env.compose pull) and
#      do not exist on a fresh checkout/restore. Without this script, a
#      restored strategy_app_* container starts with NO halt file present,
#      i.e. live-eligible by default if .env.compose's own values allow it.
#   2. FINNIFTY's buyer has never had an operator_halt at all (found during
#      this same audit -- currently harmless since FINNIFTY_EXECUTION_ADAPTER
#      is paper, but there is no reason to leave it as the one exception).
#
# Usage: run this ONCE, BEFORE `docker compose up` on any new/restored
# machine, and it is always safe to re-run (idempotent, only ever ADDS halt
# files, never removes one). It does not touch seller live-mode env vars --
# see docker-compose.seller.yml's 2026-09-16 namespacing fix for why sellers
# are separately safe-by-default now (paper/0 unless explicitly overridden).
#
# Sellers (added 2026-09-29): each seller reads operator_halt from its own
# Docker NAMED volume (/seller_run), not from .run/ -- there is no host path to
# touch. When docker is available, this also creates every seller volume
# declared in docker-compose.seller.yml (idempotent) and touches operator_halt
# inside each, so a restored or resumed seller can never come up un-halted.
#
# This script deliberately does NOT get called with any flag to "start
# live" -- resuming real trading for any instrument is a separate, explicit,
# human decision (edit .env.compose + physically remove the halt file), never
# a side effect of running this script or bringing the stack up.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_DIR="${RUN_DIR:-${REPO_ROOT}/.run}"

# Every buyer service that can hold a real-money position. Add new
# instruments here when onboarded -- do NOT rely on globbing existing
# .run/ subdirectories, since a truly fresh restore has none yet.
BUYER_SERVICES=(
  strategy_app
  strategy_app_nifty
  strategy_app_sensex
  strategy_app_midcpnifty
  strategy_app_finnifty
)

echo "[ensure_safe_defaults] RUN_DIR=${RUN_DIR}"
mkdir -p "${RUN_DIR}"

for svc in "${BUYER_SERVICES[@]}"; do
  dir="${RUN_DIR}/${svc}"
  mkdir -p "${dir}"
  halt_file="${dir}/operator_halt"
  if [ -f "${halt_file}" ]; then
    echo "[ensure_safe_defaults]   ${svc}: operator_halt already present"
  else
    touch "${halt_file}"
    echo "[ensure_safe_defaults]   ${svc}: operator_halt CREATED"
  fi
done

SELLER_COMPOSE="${REPO_ROOT}/docker-compose.seller.yml"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-option_trading}"
if command -v docker >/dev/null 2>&1 && [ -f "${SELLER_COMPOSE}" ]; then
  # Volume keys are the "<name>:/seller_run" mounts in the seller overlay.
  mapfile -t seller_vols < <(grep -oE '^[[:space:]]*- [a-z_]+:/seller_run' "${SELLER_COMPOSE}" \
    | sed -E 's/^[[:space:]]*- ([a-z_]+):.*/\1/' | sort -u)
  for key in "${seller_vols[@]}"; do
    vol="${COMPOSE_PROJECT}_${key}"
    docker volume create "${vol}" >/dev/null
    docker run --rm -v "${vol}:/seller_run" alpine sh -c \
      'if [ -f /seller_run/operator_halt ]; then echo present; else touch /seller_run/operator_halt && echo CREATED; fi' \
      | sed "s|^|[ensure_safe_defaults]   seller volume ${vol}: operator_halt |"
  done
else
  echo "[ensure_safe_defaults] WARNING: docker or ${SELLER_COMPOSE} unavailable -- seller halts NOT applied"
fi

echo "[ensure_safe_defaults] done -- every buyer and seller instrument is halted by default."
echo "[ensure_safe_defaults] To resume real trading for a specific instrument,"
echo "[ensure_safe_defaults] a human must explicitly: rm ${RUN_DIR}/<service>/operator_halt"
echo "[ensure_safe_defaults] (seller: docker exec option_trading-<seller_service>-1 rm -f /seller_run/operator_halt)"

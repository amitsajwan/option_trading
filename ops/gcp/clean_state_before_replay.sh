#!/usr/bin/env bash
# Reset ephemeral replay state before a strategy eval run (historical profile).
set -euo pipefail

REPO="${REPO_ROOT:-/opt/option_trading}"
RUN_DIR="${REPO}/.run/strategy_app_historical"
REDIS_CONTAINER="${REDIS_CONTAINER:-option_trading-redis-1}"
LOCK_KEY="strategy_app:consumer_lock:market:snapshot:v1:historical"

log() { echo "[$(date -Is)] $*"; }

log "clear stale redis consumer lock"
if [ -x "${REPO}/.venv/bin/python3" ]; then
  REPO_ROOT="${REPO}" "${REPO}/.venv/bin/python3" \
    "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
else
  sudo docker exec "${REDIS_CONTAINER}" redis-cli DEL "${LOCK_KEY}" 2>/dev/null || true
fi

log "truncate JSONL artifacts in ${RUN_DIR}"
# session_summary.jsonl is intentionally included: stale records from prior
# replay runs poison the cross-session consecutive-loss carry, causing later
# months in a multi-month window to start with risk_pause already active.
for f in positions.jsonl votes.jsonl decision_traces.jsonl signals.jsonl session_summary.jsonl; do
  path="${RUN_DIR}/${f}"
  if [ -f "${path}" ]; then
  if [ -w "${path}" ]; then
    : > "${path}"
  else
    sudo bash -c ": > '${path}'"
  fi
    log "  truncated ${f}"
  fi
done

log "clean_state done"

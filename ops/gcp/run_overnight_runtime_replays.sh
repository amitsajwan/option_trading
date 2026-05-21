#!/usr/bin/env bash
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
if [ -n "${PYTHON_BIN:-}" ]; then
  PY="${PYTHON_BIN}"
elif [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi
LOG="/tmp/overnight_pbv1_runtime.log"
exec > >(tee -a "${LOG}") 2>&1

log() { echo "[$(date -Is)] $*"; }

log "patch playbook_v1 env"
if [ -w "${REPO}/.env.compose" ]; then
  bash "${REPO}/ops/gcp/patch_playbook_v1_env.sh" "${REPO}/.env.compose"
else
  sudo bash "${REPO}/ops/gcp/patch_playbook_v1_env.sh" "${REPO}/.env.compose"
fi

log "restart strategy_app"
cd "${REPO}"
sudo docker compose --env-file "${REPO}/.env.compose" -f docker-compose.yml restart strategy_app
sleep 20

wait_run() {
  local label="$1"
  log "waiting for eval run (${label})"
  for _ in $(seq 1 600); do
    status="$("${PY}" - <<'PY'
import json, urllib.request
try:
    with urllib.request.urlopen(
        "http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical",
        timeout=15,
    ) as r:
        d = json.load(r)
    run = d if isinstance(d, dict) and d.get("run_id") else d.get("run") or d
    print(str(run.get("status") or "unknown").strip().lower())
except Exception:
    print("pending")
PY
)"
    log "  ${label} status=${status}"
    case "${status}" in
      completed|failed|cancelled) return 0 ;;
    esac
    sleep 30
  done
  log "WARN: timeout waiting for ${label}"
}

queue_window() {
  local from="$1" to="$2" label="$3"
  log "queue replay ${from} -> ${to} (${label})"
  "${PY}" "${REPO}/ops/gcp/queue_replay.py" "${from}" "${to}"
  wait_run "${label}"
}

queue_window "2024-05-01" "2024-07-31" "may_jul_2024"
queue_window "2024-08-01" "2024-10-31" "aug_oct_2024"
log "overnight runtime replays DONE"

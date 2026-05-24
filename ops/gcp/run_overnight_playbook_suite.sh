#!/usr/bin/env bash
# Overnight suite: finish PBV1 monthly post-process (ML) + optional runtime replays (PBV1 profile).
# Usage on VM:
#   tmux new-session -d -s overnight_pbv1 "bash ops/gcp/run_overnight_playbook_suite.sh ml"
#   tmux new-session -d -s overnight_pbv1_rt "bash ops/gcp/run_overnight_playbook_suite.sh runtime"
set -euo pipefail

MODE="${1:-ml}"
REPO="${REPO_ROOT:-/opt/option_trading}"
PY="${PYTHON_BIN:-${REPO}/.venv/bin/python3}"
STAMP="$(date +%Y%m%d)"
MONTHLY_ROOT="${REPO}/ml_pipeline_2/artifacts/rules_runs/playbook_v1_monthly_${STAMP}"
# Fallback to today's known run id if stamp dir missing
if [ ! -d "${MONTHLY_ROOT}" ]; then
  MONTHLY_ROOT="${REPO}/ml_pipeline_2/artifacts/rules_runs/playbook_v1_monthly_20260521"
fi

log() { echo "[$(date -Is)] $*"; }

wait_monthly() {
  log "waiting for leaderboard: ${MONTHLY_ROOT}"
  for _ in $(seq 1 360); do
    if [ -f "${MONTHLY_ROOT}/leaderboard.md" ]; then
      log "monthly DONE"
      return 0
    fi
    if ! tmux has-session -t pbv1_monthly 2>/dev/null; then
      if [ -f "${MONTHLY_ROOT}/run.log" ]; then
        tail -3 "${MONTHLY_ROOT}/run.log" || true
      fi
      log "pbv1_monthly session ended; check leaderboard manually"
      return 0
    fi
    sleep 30
  done
  log "timeout waiting for monthly"
  return 1
}

postprocess_monthly() {
  local out="${MONTHLY_ROOT}/exit_reason_summary.txt"
  log "exit reason summary -> ${out}"
  {
    echo "# Exit reasons by cell — ${MONTHLY_ROOT}"
    echo "# generated $(date -Is)"
    echo
    for rule in PBV1_TOP3_QUALITY_THESIS PBV1_TOP3_CALM_THESIS PBV1_TOP3_THESIS_TRAIL PBV1_TOP3_THESIS R1S_TOP3_S3_COMPOSITE; do
      echo "## ${rule}"
      find "${MONTHLY_ROOT}/cells" -maxdepth 1 -type d -name "${rule}_*" 2>/dev/null | sort | while read -r cell; do
        "${PY}" "${REPO}/ops/gcp/summarize_exit_reasons.py" "${cell}" 2>/dev/null | head -1 || true
      done
      echo
    done
  } | tee "${out}"
  log "leaderboard head:"
  head -40 "${MONTHLY_ROOT}/leaderboard.md" || true
}

patch_playbook_env() {
  local env="${REPO}/.env.compose"
  bash "${REPO}/ops/gcp/patch_playbook_v1_env.sh" "${env}"
}

runtime_replays() {
  patch_playbook_env
  log "restarting strategy stack (pick up STRATEGY_PROFILE_ID)"
  cd "${REPO}"
  sudo docker compose --env-file "${REPO}/.env.compose" -f docker-compose.yml restart strategy_app 2>/dev/null \
    || sudo docker compose --env-file "${REPO}/.env.compose" -f docker-compose.yml up -d strategy_app
  sleep 15
  for window in "2024-05-01 2024-07-31 pbv1_may_jul" "2024-08-01 2024-10-31 pbv1_aug_oct"; do
    set -- ${window}
    log "eval replay ${1} -> ${2}"
    "${PY}" "${REPO}/ops/gcp/queue_replay.py" "${1}" "${2}" | tee -a "/tmp/${3}.log" || true
    log "poll latest run until completed (max 4h)"
    for _ in $(seq 1 480); do
      sleep 30
      status="$("${PY}" - <<'PY' 2>/dev/null || echo pending
import json, urllib.request
r = urllib.request.urlopen("http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical", timeout=10)
d = json.load(r)
print(d.get("status") or d.get("run", {}).get("status") or "unknown")
PY
)"
      log "  status=${status}"
      if [ "${status}" = "completed" ] || [ "${status}" = "failed" ]; then
        break
      fi
    done
  done
}

case "${MODE}" in
  ml)
    wait_monthly
    postprocess_monthly
    ;;
  runtime)
    if [ ! -f "${REPO}/strategy_app/engines/playbook_brain.py" ]; then
      log "ERROR: playbook_brain.py missing on runtime — sync code before runtime mode"
      exit 1
    fi
    runtime_replays
    ;;
  *)
    echo "usage: $0 ml|runtime" >&2
    exit 2
    ;;
esac

log "overnight suite (${MODE}) finished"

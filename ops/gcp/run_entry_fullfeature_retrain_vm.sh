#!/usr/bin/env bash
# Full-feature entry-model retrain (ENTRY_MODEL_FULLFEATURE_HANDOVER.md).
#
# For each label X in {0.10%, 0.13%, 0.20%}:
#   1. run the comprehensive-feature entry S1 HPO (fo_comprehensive only),
#   2. isotonic-calibrate the winner on the held-out 2024-05..07 window,
#   3. evaluate the handover ship-gates on the 2024-08..10 OOS holdout,
#   4. write a calibrated entry_only_bundle + report.
#
# Designed to be launched detached on the VM (off live market hours):
#   sudo bash ops/gcp/run_entry_fullfeature_retrain_vm.sh start
#   bash ops/gcp/run_entry_fullfeature_retrain_vm.sh status
#
# NOTE: never installs the bundle as active automatically — strategy paper-validates
# the winning bundle (net after cost + drop-outlier) before any --set-active.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
RUN_USER="${RUN_USER:-amits}"
TMUX_SESSION="${TMUX_SESSION:-entry_fullfeature}"
SKIP_COMPOSE_DOWN="${SKIP_COMPOSE_DOWN:-0}"
TARGET_FIRE="${TARGET_FIRE:-0.25}"

# Re-exec as repo owner (avoid root-owned artifacts); stop compose to free RAM.
if [[ "$(id -u)" -eq 0 ]] && [[ -z "${RETRAIN_REEXEC:-}" ]]; then
  if [[ "${SKIP_COMPOSE_DOWN}" != "1" && "${1:-start}" == "start" ]] && [[ -f "${REPO_ROOT}/docker-compose.gcp.yml" ]]; then
    cd "${REPO_ROOT}"
    docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml down || true
  fi
  mkdir -p "${REPO_ROOT}/ml_pipeline_2/artifacts/research"
  chown -R "${RUN_USER}:${RUN_USER}" "${REPO_ROOT}/ml_pipeline_2/artifacts" 2>/dev/null || true
  export RETRAIN_REEXEC=1
  exec sudo -u "${RUN_USER}" -H \
    RETRAIN_REEXEC=1 REPO_ROOT="${REPO_ROOT}" RUN_USER="${RUN_USER}" \
    TMUX_SESSION="${TMUX_SESSION}" SKIP_COMPOSE_DOWN="${SKIP_COMPOSE_DOWN}" TARGET_FIRE="${TARGET_FIRE}" \
    bash "${REPO_ROOT}/ops/gcp/run_entry_fullfeature_retrain_vm.sh" "${@:-start}"
fi

cd "${REPO_ROOT}"
export PYTHONPATH="${REPO_ROOT}"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

CFG_DIR="ml_pipeline_2/configs/research"
RESEARCH_ROOT="ml_pipeline_2/artifacts/research"
PUBLISH_ROOT="ml_pipeline_2/artifacts/entry_only/published_comprehensive"
LOG_ROOT="${LOG_ROOT:-/tmp/entry_fullfeature_retrain}"
STATE_DIR="${LOG_ROOT}/state"
MASTER_LOG="${LOG_ROOT}/master.log"
PIDFILE="${LOG_ROOT}/orchestrator.pid"
PHASEFILE="${STATE_DIR}/phase.txt"
mkdir -p "${STATE_DIR}" "${PUBLISH_ROOT}"

# tag : manifest : min_pct
LABELS=(
  "010pct:staged_dual_recipe.entry_s1_comprehensive_5m_010pct.json:0.0010"
  "013pct:staged_dual_recipe.entry_s1_comprehensive_5m_013pct.json:0.0013"
  "020pct:staged_dual_recipe.entry_s1_comprehensive_5m_020pct.json:0.0020"
)

_log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "${MASTER_LOG}"; }

_run_one() {
  local tag="$1" manifest="$2" min_pct="$3"
  local run_name="entry_s1_comprehensive_5m_${tag}"
  local run_dir="${RESEARCH_ROOT}/${run_name}"
  local hpo_log="${LOG_ROOT}/hpo_${tag}.log"
  local pub_log="${LOG_ROOT}/publish_${tag}.log"

  echo "hpo_${tag}" > "${PHASEFILE}"
  _log "START hpo ${tag} (min_pct=${min_pct})"
  "${VENV_PYTHON}" -u -m ml_pipeline_2.scripts.run_entry_s1_only_hpo \
    --config "${CFG_DIR}/${manifest}" \
    --run-output-root "${run_dir}" \
    --run-reuse-mode restart > "${hpo_log}" 2>&1 || { _log "HPO ${tag} FAILED (see ${hpo_log})"; return 1; }
  _log "DONE hpo ${tag}"

  echo "publish_${tag}" > "${PHASEFILE}"
  _log "START publish+ship-gates ${tag}"
  set +e
  "${VENV_PYTHON}" -u -m ml_pipeline_2.scripts.publish_entry_calibrated \
    --run-dir "${run_dir}" \
    --min-pct "${min_pct}" \
    --label-tag "comprehensive_${tag}" \
    --feature-set-label fo_comprehensive \
    --target-fire "${TARGET_FIRE}" \
    --output "${PUBLISH_ROOT}/entry_only_model_${tag}.joblib" > "${pub_log}" 2>&1
  local rc=$?
  set -e
  _log "publish ${tag} rc=${rc} (0=ALL_PASS, 2=gates FAIL)"
  grep -E "holdout AUC|operating thr|drop-outlier|entries/day|gates:" "${pub_log}" | sed "s/^/[${tag}] /" | tee -a "${MASTER_LOG}" || true
  return 0
}

_orchestrate() {
  _log "RETRAIN start user=$(whoami) target_fire=${TARGET_FIRE}"
  local failures=0
  for spec in "${LABELS[@]}"; do
    IFS=':' read -r tag manifest min_pct <<< "${spec}"
    _run_one "${tag}" "${manifest}" "${min_pct}" || failures=$((failures+1))
  done
  echo "complete" > "${PHASEFILE}"
  _log "RETRAIN complete (hpo failures=${failures}). Reports under ${PUBLISH_ROOT}/*_report.json"
  _log "Pick the bundle with ship_gates_all_pass=true that beats entry_only_v3/020pct on separation + drop-outlier."
}

_status() {
  echo "=== entry full-feature retrain ==="
  [[ -f "${PHASEFILE}" ]] && echo "phase=$(cat "${PHASEFILE}")"
  if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "orchestrator RUNNING pid=$(cat "${PIDFILE}")"
  else
    echo "orchestrator NOT RUNNING"
  fi
  echo "--- master (last 25) ---"
  tail -25 "${MASTER_LOG}" 2>/dev/null || true
}

case "${1:-start}" in
  status) _status ;;
  _foreground)
    # Internal entrypoint: runs the orchestrator in the FOREGROUND so the
    # surrounding tmux (or nohup) session stays alive for the whole run.
    echo "$$" > "${PIDFILE}"
    trap 'rm -f "${PIDFILE}"' EXIT
    _orchestrate
    ;;
  start)
    if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}" 2>/dev/null || echo)" 2>/dev/null; then
      echo "Already running pid=$(cat "${PIDFILE}"). Use: $0 status"; exit 1
    fi
    if [[ -z "${TMUX:-}" ]] && command -v tmux >/dev/null 2>&1 && [[ -z "${RETRAIN_NO_TMUX:-}" ]]; then
      if tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        echo "tmux session ${TMUX_SESSION} exists. Attach: tmux attach -t ${TMUX_SESSION}"; exit 1
      fi
      # Run the orchestrator in the FOREGROUND of the detached session. The
      # session lives exactly as long as _orchestrate, keeping the python HPO
      # (a child of this session) alive until completion.
      tmux new-session -d -s "${TMUX_SESSION}" \
        "RETRAIN_NO_TMUX=1 bash ${REPO_ROOT}/ops/gcp/run_entry_fullfeature_retrain_vm.sh _foreground >> ${MASTER_LOG} 2>&1"
      sleep 1
      echo "Started in tmux ${TMUX_SESSION} pid=$(cat "${PIDFILE}" 2>/dev/null || echo pending). Status: bash $0 status"; exit 0
    fi
    # No-tmux fallback: detach via nohup+setsid so SIGHUP on shell exit can't kill it.
    setsid nohup bash "${REPO_ROOT}/ops/gcp/run_entry_fullfeature_retrain_vm.sh" _foreground >> "${MASTER_LOG}" 2>&1 < /dev/null &
    disown 2>/dev/null || true
    sleep 1
    echo "Started pid=$(cat "${PIDFILE}" 2>/dev/null || echo unknown) log=${MASTER_LOG}"
    ;;
  *) echo "Usage: $0 {start|status}" >&2; exit 2 ;;
esac

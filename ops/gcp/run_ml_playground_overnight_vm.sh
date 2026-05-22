#!/usr/bin/env bash
# ML playground overnight: entry + direction HPO and feature-set grids.
#
# Usage:
#   bash ops/gcp/run_ml_playground_overnight_vm.sh preflight
#   bash ops/gcp/run_ml_playground_overnight_vm.sh validate
#   bash ops/gcp/run_ml_playground_overnight_vm.sh start    # tmux detach-safe
#   bash ops/gcp/run_ml_playground_overnight_vm.sh status
#
# Env:
#   PLAYGROUND_MODE=all|hpo|grid   (default: all)
#   SKIP_COMPOSE_DOWN=1
#   CONTINUE_ON_PHASE_ERROR=1      (default: continue to next phase)
#   RUN_USER=amits                   (python runs as this user)
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
PLAYGROUND_MODE="${PLAYGROUND_MODE:-all}"
SKIP_COMPOSE_DOWN="${SKIP_COMPOSE_DOWN:-0}"
CONTINUE_ON_PHASE_ERROR="${CONTINUE_ON_PHASE_ERROR:-1}"
RUN_USER="${RUN_USER:-amits}"
TMUX_SESSION="${TMUX_SESSION:-ml_playground}"

# Re-exec as repo owner for Python (avoid root-owned artifacts). Stop compose as root first.
if [[ "$(id -u)" -eq 0 ]] && [[ -z "${PLAYGROUND_REEXEC:-}" ]]; then
  if [[ "${SKIP_COMPOSE_DOWN}" != "1" && "${1:-start}" == "start" ]] && [[ -f "${REPO_ROOT}/docker-compose.gcp.yml" ]]; then
    cd "${REPO_ROOT}"
    docker compose --env-file .env.compose \
      -f docker-compose.yml -f docker-compose.gcp.yml down || true
  fi
  export PLAYGROUND_REEXEC=1
  exec sudo -u "${RUN_USER}" -H \
    PLAYGROUND_REEXEC=1 REPO_ROOT="${REPO_ROOT}" PLAYGROUND_MODE="${PLAYGROUND_MODE}" \
    SKIP_COMPOSE_DOWN="${SKIP_COMPOSE_DOWN}" CONTINUE_ON_PHASE_ERROR="${CONTINUE_ON_PHASE_ERROR}" \
    RUN_USER="${RUN_USER}" TMUX_SESSION="${TMUX_SESSION}" \
    bash "${REPO_ROOT}/ops/gcp/run_ml_playground_overnight_vm.sh" "${@:-start}"
fi

cd "${REPO_ROOT}"
export PYTHONPATH="${REPO_ROOT}"

VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
MODEL_GROUP="${MODEL_GROUP:-banknifty_futures/h15_tp_auto}"
PROFILE_ID="${PROFILE_ID:-ml_playground_research_v1}"

ENTRY_HPO_MANIFEST="${ENTRY_HPO_MANIFEST:-ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_only_hpo_v2.json}"
DIR_HPO_MANIFEST="${DIR_HPO_MANIFEST:-ml_pipeline_2/configs/research/staged_dual_recipe.direction_s2_only_hpo_v2.json}"
ENTRY_GRID_MANIFEST="${ENTRY_GRID_MANIFEST:-ml_pipeline_2/configs/research/staged_grid.entry_playground_v1.json}"
DIR_GRID_MANIFEST="${DIR_GRID_MANIFEST:-ml_pipeline_2/configs/research/staged_grid.direction_playground_v1.json}"

LOG_ROOT="${LOG_ROOT:-/tmp/ml_playground_overnight}"
STATE_DIR="${LOG_ROOT}/state"
MASTER_LOG="${LOG_ROOT}/master.log"
PIDFILE="${LOG_ROOT}/orchestrator.pid"
PHASEFILE="${STATE_DIR}/phase.txt"
FAILFILE="${STATE_DIR}/failures.txt"

mkdir -p "${STATE_DIR}" "${LOG_ROOT}"
: > "${FAILFILE}"

_log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "${MASTER_LOG}"
}

_record_failure() {
  echo "$1" >> "${FAILFILE}"
  _log "PHASE FAILED: $1"
}

_tail_log_on_fail() {
  local logfile="$1"
  if [[ -f "${logfile}" ]]; then
    _log "--- tail ${logfile} ---"
    tail -40 "${logfile}" >> "${MASTER_LOG}" 2>&1 || true
  fi
}

_run_phase() {
  local label="$1"
  shift
  local logfile="$1"
  shift
  echo "${label}" > "${PHASEFILE}"
  _log "START ${label}"
  set +e
  "$@" >> "${logfile}" 2>&1
  local rc=$?
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    _record_failure "${label} exit=${rc}"
    _tail_log_on_fail "${logfile}"
    if [[ "${CONTINUE_ON_PHASE_ERROR}" != "1" ]]; then
      _log "ABORT (CONTINUE_ON_PHASE_ERROR=0)"
      return "${rc}"
    fi
    _log "CONTINUE after ${label} failure"
    return 0
  fi
  _log "DONE ${label}"
  return 0
}

_run_entry_hpo() {
  _run_phase "entry_hpo" "${LOG_ROOT}/01_entry_hpo.log" \
    "${VENV_PYTHON}" -u -m ml_pipeline_2.scripts.run_entry_s1_only_hpo \
    --config "${ENTRY_HPO_MANIFEST}"
}

_run_direction_hpo() {
  _run_phase "direction_hpo" "${LOG_ROOT}/02_direction_hpo.log" \
    "${VENV_PYTHON}" -u -m ml_pipeline_2.scripts.run_direction_s2_only_hpo \
    --config "${DIR_HPO_MANIFEST}"
}

_run_entry_grid() {
  _run_phase "entry_grid" "${LOG_ROOT}/03_entry_grid.log" \
    "${VENV_PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${ENTRY_GRID_MANIFEST}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}"
}

_run_direction_grid() {
  _run_phase "direction_grid" "${LOG_ROOT}/04_direction_grid.log" \
    "${VENV_PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${DIR_GRID_MANIFEST}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}"
}

_compose_down() {
  if [[ "${SKIP_COMPOSE_DOWN}" == "1" ]]; then
    _log "SKIP_COMPOSE_DOWN=1"
    return 0
  fi
  if [[ ! -f docker-compose.gcp.yml ]]; then
    return 0
  fi
  _log "Stopping compose (free RAM)"
  if [[ "$(id -u)" -eq 0 ]]; then
    docker compose --env-file .env.compose \
      -f docker-compose.yml -f docker-compose.gcp.yml down || true
  else
    sudo docker compose --env-file .env.compose \
      -f docker-compose.yml -f docker-compose.gcp.yml down || true
  fi
}

_summarize() {
  echo "summarize" > "${PHASEFILE}"
  if [[ -f "${REPO_ROOT}/ops/gcp/summarize_ml_playground_overnight.sh" ]]; then
    bash "${REPO_ROOT}/ops/gcp/summarize_ml_playground_overnight.sh" | tee -a "${MASTER_LOG}" || true
  fi
}

_status() {
  echo "=== ML playground overnight ==="
  echo "mode=${PLAYGROUND_MODE} user=$(whoami) log_root=${LOG_ROOT}"
  [[ -f "${PHASEFILE}" ]] && echo "phase=$(cat "${PHASEFILE}")"
  if [[ -f "${FAILFILE}" ]] && [[ -s "${FAILFILE}" ]]; then
    echo "failures:"
    cat "${FAILFILE}"
  fi
  if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "orchestrator RUNNING pid=$(cat "${PIDFILE}")"
  else
    echo "orchestrator NOT RUNNING"
  fi
  echo "--- master (last 20) ---"
  tail -20 "${MASTER_LOG}" 2>/dev/null || true
  for f in 01_entry_hpo.log 02_direction_hpo.log 03_entry_grid.log 04_direction_grid.log; do
    [[ -f "${LOG_ROOT}/${f}" ]] && echo "--- ${f} (last 3) ---" && tail -3 "${LOG_ROOT}/${f}" 2>/dev/null || true
  done
}

_orchestrate() {
  _log "PLAYGROUND start mode=${PLAYGROUND_MODE} user=$(whoami)"
  bash "${REPO_ROOT}/ops/gcp/preflight_ml_playground.sh"
  _compose_down

  case "${PLAYGROUND_MODE}" in
    hpo)
      _run_entry_hpo
      _run_direction_hpo
      ;;
    grid)
      _run_entry_grid
      _run_direction_grid
      ;;
    all)
      _run_entry_hpo
      _run_direction_hpo
      _run_entry_grid
      _run_direction_grid
      ;;
    *)
      echo "Unknown PLAYGROUND_MODE=${PLAYGROUND_MODE}" >&2
      exit 2
      ;;
  esac

  _summarize
  if [[ -s "${FAILFILE}" ]]; then
    echo "failed" > "${PHASEFILE}"
    _log "PLAYGROUND finished WITH FAILURES — see ${FAILFILE}"
    exit 1
  fi
  echo "complete" > "${PHASEFILE}"
  _log "PLAYGROUND complete (all phases OK)"
}

_cmd="${1:-start}"
case "${_cmd}" in
  preflight|validate)
    bash "${REPO_ROOT}/ops/gcp/preflight_ml_playground.sh"
    ;;
  status)
    _status
    ;;
  start)
    if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
      echo "Already running pid=$(cat "${PIDFILE}"). Use: $0 status"
      exit 1
    fi
    if [[ -z "${TMUX:-}" ]] && command -v tmux >/dev/null 2>&1 && [[ -z "${PLAYGROUND_NO_TMUX:-}" ]]; then
      if tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        echo "tmux session ${TMUX_SESSION} exists. Attach: tmux attach -t ${TMUX_SESSION}"
        exit 1
      fi
      tmux new-session -d -s "${TMUX_SESSION}" \
        "PLAYGROUND_NO_TMUX=1 bash ${REPO_ROOT}/ops/gcp/run_ml_playground_overnight_vm.sh start"
      echo "Started in tmux session ${TMUX_SESSION}"
      echo "Attach: tmux attach -t ${TMUX_SESSION}"
      echo "Status: bash ${REPO_ROOT}/ops/gcp/run_ml_playground_overnight_vm.sh status"
      exit 0
    fi
    (_orchestrate) >> "${MASTER_LOG}" 2>&1 &
    echo $! > "${PIDFILE}"
    echo "Started pid=$(cat "${PIDFILE}") mode=${PLAYGROUND_MODE} log=${MASTER_LOG}"
    ;;
  *)
    echo "Usage: $0 {preflight|validate|start|status}" >&2
    exit 2
    ;;
esac

#!/usr/bin/env bash
# DUAL signed-entry retrain: CE (up) + PE (down) — your "two models, regime picks
# which runs" design. Same X%/Y-min shape as the magnitude model, but signed:
#   CE = P(forward-5min HIGH clears +0.13%);  PE = P(forward-5min LOW clears -0.13%).
#
# Fully automated / disconnect-proof (off-market overnight):
#   1. WAITS for the magnitude full-feature run (entry_fullfeature) to finish so
#      they don't fight for the 8 cores.
#   2. For each side: comprehensive-feature HPO -> isotonic-calibrate -> ship-gates
#      -> write a calibrated entry_only_bundle. AUTO-PUBLISHES every gate result
#      (writes bundle + report); does NOT hold. Never --set-active (live cutover
#      stays a human morning decision).
#   3. Runs in a tmux FOREGROUND so it survives ssh drops.
#
#   sudo RUN_USER=ubuntu bash ops/gcp/run_dual_entry_retrain_vm.sh start
#   bash ops/gcp/run_dual_entry_retrain_vm.sh status
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
RUN_USER="${RUN_USER:-amits}"
TMUX_SESSION="${TMUX_SESSION:-dual_entry}"
TARGET_FIRE="${TARGET_FIRE:-0.25}"
# Wait for this phase file to read 'complete' before starting (magnitude run).
WAIT_PHASE="${WAIT_PHASE:-/tmp/entry_fullfeature_retrain/state/phase.txt}"
WAIT_MAX_SECONDS="${WAIT_MAX_SECONDS:-25200}"   # 7h hard cap, then proceed anyway

if [[ "$(id -u)" -eq 0 ]] && [[ -z "${RETRAIN_REEXEC:-}" ]]; then
  mkdir -p "${REPO_ROOT}/ml_pipeline_2/artifacts/research"
  chown -R "${RUN_USER}:${RUN_USER}" "${REPO_ROOT}/ml_pipeline_2/artifacts" 2>/dev/null || true
  export RETRAIN_REEXEC=1
  exec sudo -u "${RUN_USER}" -H \
    RETRAIN_REEXEC=1 REPO_ROOT="${REPO_ROOT}" RUN_USER="${RUN_USER}" \
    TMUX_SESSION="${TMUX_SESSION}" TARGET_FIRE="${TARGET_FIRE}" \
    WAIT_PHASE="${WAIT_PHASE}" WAIT_MAX_SECONDS="${WAIT_MAX_SECONDS}" \
    bash "${REPO_ROOT}/ops/gcp/run_dual_entry_retrain_vm.sh" "${@:-start}"
fi

cd "${REPO_ROOT}"
export PYTHONPATH="${REPO_ROOT}"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

CFG_DIR="ml_pipeline_2/configs/research"
RESEARCH_ROOT="ml_pipeline_2/artifacts/research"
PUBLISH_ROOT="ml_pipeline_2/artifacts/entry_only/published_dual"
LOG_ROOT="${LOG_ROOT:-/tmp/dual_entry_retrain}"
STATE_DIR="${LOG_ROOT}/state"
MASTER_LOG="${LOG_ROOT}/master.log"
PIDFILE="${LOG_ROOT}/orchestrator.pid"
PHASEFILE="${STATE_DIR}/phase.txt"
mkdir -p "${STATE_DIR}" "${PUBLISH_ROOT}"

# tag : manifest : min_pct : side
SIDES=(
  "ce_013:staged_dual_recipe.entry_s1_dual_ce_5m_013pct.json:0.0013:up"
  "pe_013:staged_dual_recipe.entry_s1_dual_pe_5m_013pct.json:0.0013:down"
)

_log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "${MASTER_LOG}"; }

_wait_for_magnitude() {
  local waited=0
  if [[ ! -f "${WAIT_PHASE}" ]]; then
    _log "no magnitude phase file at ${WAIT_PHASE}; not waiting"
    return 0
  fi
  _log "waiting for magnitude run to reach phase=complete (cap ${WAIT_MAX_SECONDS}s)"
  while [[ "$(cat "${WAIT_PHASE}" 2>/dev/null || echo)" != "complete" ]]; do
    sleep 120; waited=$((waited+120))
    if [[ "${waited}" -ge "${WAIT_MAX_SECONDS}" ]]; then
      _log "WAIT cap hit (${waited}s); proceeding despite magnitude phase=$(cat "${WAIT_PHASE}" 2>/dev/null || echo ?)"
      return 0
    fi
  done
  _log "magnitude run complete after ${waited}s wait; starting dual retrain"
}

_run_one() {
  local tag="$1" manifest="$2" min_pct="$3" side="$4"
  local run_name="entry_s1_dual_${tag}"
  local run_dir="${RESEARCH_ROOT}/${run_name}"
  local hpo_log="${LOG_ROOT}/hpo_${tag}.log"
  local pub_log="${LOG_ROOT}/publish_${tag}.log"

  echo "hpo_${tag}" > "${PHASEFILE}"
  _log "START hpo ${tag} (side=${side} min_pct=${min_pct})"
  "${VENV_PYTHON}" -u -m ml_pipeline_2.scripts.run_entry_s1_only_hpo \
    --config "${CFG_DIR}/${manifest}" \
    --run-output-root "${run_dir}" \
    --run-reuse-mode restart > "${hpo_log}" 2>&1 || { _log "HPO ${tag} FAILED (see ${hpo_log})"; return 1; }
  _log "DONE hpo ${tag}"

  echo "publish_${tag}" > "${PHASEFILE}"
  _log "START publish+ship-gates ${tag} (--side ${side}) [AUTO-PUBLISH, no hold]"
  set +e
  "${VENV_PYTHON}" -u -m ml_pipeline_2.scripts.publish_entry_calibrated \
    --run-dir "${run_dir}" \
    --min-pct "${min_pct}" \
    --side "${side}" \
    --label-tag "dual_${tag}" \
    --feature-set-label fo_comprehensive \
    --target-fire "${TARGET_FIRE}" \
    --output "${PUBLISH_ROOT}/entry_only_model_${tag}.joblib" > "${pub_log}" 2>&1
  local rc=$?
  set -e
  _log "publish ${tag} rc=${rc} (0=ALL_PASS bundle written, 2=gates FAIL bundle still written)"
  grep -E "holdout AUC|operating thr|drop-outlier|entries/day|gates:" "${pub_log}" | sed "s/^/[${tag}] /" | tee -a "${MASTER_LOG}" || true
  return 0
}

_orchestrate() {
  _log "DUAL retrain start user=$(whoami) target_fire=${TARGET_FIRE}"
  _wait_for_magnitude
  local failures=0
  for spec in "${SIDES[@]}"; do
    IFS=':' read -r tag manifest min_pct side <<< "${spec}"
    _run_one "${tag}" "${manifest}" "${min_pct}" "${side}" || failures=$((failures+1))
  done
  echo "complete" > "${PHASEFILE}"
  _log "DUAL retrain complete (hpo failures=${failures}). Bundles + *_report.json under ${PUBLISH_ROOT}/"
  _log "MORNING: compare CE vs PE per-side AUC + fired-bar follow-through; pick regime gate; --set-active is still a manual decision."
}

_status() {
  echo "=== dual signed-entry retrain ==="
  [[ -f "${PHASEFILE}" ]] && echo "phase=$(cat "${PHASEFILE}")"
  if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}" 2>/dev/null || echo)" 2>/dev/null; then
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
      tmux new-session -d -s "${TMUX_SESSION}" \
        "RETRAIN_NO_TMUX=1 bash ${REPO_ROOT}/ops/gcp/run_dual_entry_retrain_vm.sh _foreground >> ${MASTER_LOG} 2>&1"
      sleep 1
      echo "Started in tmux ${TMUX_SESSION} pid=$(cat "${PIDFILE}" 2>/dev/null || echo pending). Status: bash $0 status"; exit 0
    fi
    setsid nohup bash "${REPO_ROOT}/ops/gcp/run_dual_entry_retrain_vm.sh" _foreground >> "${MASTER_LOG}" 2>&1 < /dev/null &
    disown 2>/dev/null || true
    sleep 1
    echo "Started pid=$(cat "${PIDFILE}" 2>/dev/null || echo unknown) log=${MASTER_LOG}"
    ;;
  *) echo "Usage: $0 {start|status}" >&2; exit 2 ;;
esac

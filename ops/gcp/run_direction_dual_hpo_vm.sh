#!/usr/bin/env bash
# Train dual direction models (CE + PE) and export direction_dual_bundle on the runtime VM.
#
# Stops all docker compose services first to free resources, then runs both HPO
# runs sequentially (CE then PE) and exports the dual bundle.
#
# Usage:
#   sudo bash ops/gcp/run_direction_dual_hpo_vm.sh            # start
#   sudo bash ops/gcp/run_direction_dual_hpo_vm.sh status     # check progress
#   sudo bash ops/gcp/run_direction_dual_hpo_vm.sh validate   # dry-run
set -euo pipefail

REPO="${REPO_ROOT:-/opt/option_trading}"
PY="${REPO}/.venv/bin/python3"
LOG=/tmp/direction_dual_hpo.log
PIDFILE=/tmp/direction_dual_hpo.pid
ARTIFACTS_ROOT="${REPO}/ml_pipeline_2/artifacts/research"
CE_CONFIG="${CE_CONFIG:-${REPO}/ml_pipeline_2/configs/research/direction_dual_ce_hpo_v1.json}"
PE_CONFIG="${PE_CONFIG:-${REPO}/ml_pipeline_2/configs/research/direction_dual_pe_hpo_v1.json}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO}/ml_pipeline_2/artifacts/direction_dual/published}"

if [[ "${1:-}" == "status" ]]; then
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "RUNNING pid=$(cat "$PIDFILE")"
    tail -30 "$LOG"
  else
    echo "NOT RUNNING"
    tail -40 "$LOG" 2>/dev/null || true
  fi
  exit 0
fi

if [[ "${1:-}" == "validate" ]]; then
  echo "=== Validating CE config ==="
  cd "${REPO}" && PYTHONPATH="${REPO}" "${PY}" -m ml_pipeline_2.scripts.run_direction_s2_only_hpo \
    --config "${CE_CONFIG}" --validate-only
  echo "=== Validating PE config ==="
  cd "${REPO}" && PYTHONPATH="${REPO}" "${PY}" -m ml_pipeline_2.scripts.run_direction_s2_only_hpo \
    --config "${PE_CONFIG}" --validate-only
  exit 0
fi

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running pid=$(cat "$PIDFILE"). Use: $0 status"
  exit 1
fi

cd "${REPO}"

# Stop docker compose to free resources
echo "[$(date -Is)] Stopping docker compose services..."
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
  --profile historical --profile dashboard --profile strategy_eval down --timeout 30 2>&1 | tail -5 || true
sudo docker ps -q | xargs -r sudo docker stop 2>/dev/null || true
echo "[$(date -Is)] Docker stopped."

# Write worker script (no heredoc — use explicit file writes to avoid escaping issues)
WORKER=/tmp/direction_dual_hpo_worker.sh
printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "${WORKER}"
printf 'REPO="%s"\n' "${REPO}" >> "${WORKER}"
printf 'PY="%s"\n' "${PY}" >> "${WORKER}"
printf 'CE_CONFIG="%s"\n' "${CE_CONFIG}" >> "${WORKER}"
printf 'PE_CONFIG="%s"\n' "${PE_CONFIG}" >> "${WORKER}"
printf 'OUTPUT_DIR="%s"\n' "${OUTPUT_DIR}" >> "${WORKER}"
printf 'ARTIFACTS_ROOT="%s/ml_pipeline_2/artifacts/research"\n' "${REPO}" >> "${WORKER}"
printf 'export PYTHONPATH="${REPO}"\n' >> "${WORKER}"
printf 'cd "${REPO}"\n' >> "${WORKER}"
cat >> "${WORKER}" << 'ENDWORKER'

_check_dual_run() {
  local run_dir="$1"
  local side="$2"
  local mode
  mode="$(python3 -c "import json; print(json.load(open('${run_dir}/summary.json')).get('completion_mode',''))")"
  if [[ "${mode}" != "completed" ]]; then
    echo "ERROR: ${side} run did not complete training (completion_mode=${mode}): ${run_dir}"
    python3 -c "import json; s=json.load(open('${run_dir}/summary.json')); print(s.get('publish_assessment',{}))"
    exit 1
  fi
  if [[ ! -f "${run_dir}/stages/stage2/model.joblib" ]]; then
    echo "ERROR: ${side} missing stages/stage2/model.joblib under ${run_dir}"
    exit 1
  fi
}

echo "[$(date -Is)] === Direction Dual HPO: model_CE ==="
"${PY}" -m ml_pipeline_2.scripts.run_direction_s2_only_hpo --config "${CE_CONFIG}"

CE_RUN_DIR="$(ls -td "${ARTIFACTS_ROOT}"/direction_dual_ce_hpo_v1_* 2>/dev/null | head -1)"
[ -n "${CE_RUN_DIR}" ] || { echo "ERROR: CE run dir not found under ${ARTIFACTS_ROOT}"; exit 1; }
_check_dual_run "${CE_RUN_DIR}" "CE"
echo "[$(date -Is)] CE done: ${CE_RUN_DIR}"

echo "[$(date -Is)] === Direction Dual HPO: model_PE ==="
"${PY}" -m ml_pipeline_2.scripts.run_direction_s2_only_hpo --config "${PE_CONFIG}"

PE_RUN_DIR="$(ls -td "${ARTIFACTS_ROOT}"/direction_dual_pe_hpo_v1_* 2>/dev/null | head -1)"
[ -n "${PE_RUN_DIR}" ] || { echo "ERROR: PE run dir not found under ${ARTIFACTS_ROOT}"; exit 1; }
_check_dual_run "${PE_RUN_DIR}" "PE"
echo "[$(date -Is)] PE done: ${PE_RUN_DIR}"

echo "[$(date -Is)] === Exporting direction_dual_bundle ==="
"${PY}" -m ml_pipeline_2.scripts.export_direction_dual_bundle \
  --ce-run-dir "${CE_RUN_DIR}" \
  --pe-run-dir "${PE_RUN_DIR}" \
  --output-dir "${OUTPUT_DIR}"

echo ""
echo "=== DONE ==="
echo "Next: sudo bash ops/gcp/run_engine_direction_ab.sh v1_dual_direction_ml"
ENDWORKER

chmod +x "${WORKER}"
nohup bash "${WORKER}" >> "${LOG}" 2>&1 &
echo $! > "${PIDFILE}"

echo "Started dual direction HPO pid=$(cat "${PIDFILE}") log=${LOG}"
echo "Monitor: tail -f ${LOG}"
echo "Status:  sudo bash ops/gcp/run_direction_dual_hpo_vm.sh status"

#!/usr/bin/env bash
# Forensics-driven experiments on oos_primary (May–Jul 2024).
#
#   sudo bash ops/gcp/run_forensics_experiments.sh [expA|expB|expC|all]
#
#   A  v1_direction_ml + eval defaults (session cap=12)
#   B  v1_direction_ml + no session cap (unlock missed entries)
#   C  dyn_exit v2 + no session cap
#
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${REPO}/.env.compose"
LOG_DIR="${REPO}/.run/forensics_experiments"
MODE="${1:-all}"
mkdir -p "${LOG_DIR}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

wait_hist() {
  cd "${REPO}"
  REPO_ROOT="${REPO}" "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
  sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate --pull never strategy_app_historical
  sleep 20
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 180 || true
}

analyze_run() {
  local tag="$1"
  local rid="$2"
  local log="${LOG_DIR}/${tag}.log"
  sudo docker cp "${REPO}/ops/gcp/analyze_trade_forensics.py" option_trading-dashboard-1:/tmp/analyze_trade_forensics.py 2>/dev/null || true
  sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_run.py" option_trading-dashboard-1:/tmp/analyze_oos_validation_run.py 2>/dev/null || true
  {
    echo ""
    echo "======== analyze ${tag} run_id=${rid} ========"
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/analyze_oos_validation_run.py "${rid}" "oos_${tag}" || true
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/analyze_trade_forensics.py --run-id "${rid}" \
      --date-from 2024-05-01 --date-to 2024-07-31 --top 8 || true
  } >> "${log}" 2>&1
}

run_exp() {
  local tag="$1"
  local profile_patch="$2"
  local unlock="${3:-0}"
  local log="${LOG_DIR}/${tag}.log"
  echo "[$(date -Is)] START ${tag}" | tee -a "${LOG_DIR}/master.log"
  sudo bash "${REPO}/ops/gcp/clean_state_before_replay.sh" >> "${log}" 2>&1
  sudo bash "${profile_patch}" "${ENV_FILE}" >> "${log}" 2>&1
  sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" >> "${log}" 2>&1
  if [ "${unlock}" = "1" ]; then
    sudo bash "${REPO}/ops/gcp/patch_eval_replay_unlock_gates.sh" "${ENV_FILE}" >> "${log}" 2>&1
  fi
  wait_hist >> "${log}" 2>&1
  sudo OOS_REPLAY_SKIP_ENV_PATCH=1 bash "${REPO}/ops/gcp/run_oos_validation_replay.sh" replay_only oos_primary >> "${log}" 2>&1
  local rid
  rid="$("${PY}" -c "
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical', timeout=20) as r:
    d = json.load(r)
run = d if d.get('run_id') else d.get('run') or d
print(run.get('run_id',''))
")"
  echo "${tag}=${rid}" | tee -a "${LOG_DIR}/run_ids.env"
  analyze_run "${tag}" "${rid}"
  echo "[$(date -Is)] DONE ${tag} run_id=${rid}" | tee -a "${LOG_DIR}/master.log"
}

cd "${REPO}"
git pull --ff-only origin main 2>/dev/null || true

case "${MODE}" in
  expA) run_exp "expA_v1_dir_baseline" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" 0 ;;
  expB) run_exp "expB_v1_dir_no_cap" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" 1 ;;
  expC) run_exp "expC_v1_dyn_exit_v2" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dyn_exit_env.sh" 1 ;;
  all)
    run_exp "expA_v1_dir_baseline" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" 0
    run_exp "expB_v1_dir_no_cap" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" 1
    run_exp "expC_v1_dyn_exit_v2" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dyn_exit_env.sh" 1
    ;;
  *) echo "Usage: $0 [expA|expB|expC|all]"; exit 2 ;;
esac

echo "[$(date -Is)] finished — logs in ${LOG_DIR}/"

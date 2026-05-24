#!/usr/bin/env bash
# Exit + risk experiments only (frozen ML entry + direction ML).
#
#   sudo bash ops/gcp/run_exit_risk_experiments.sh [E1|E2|E3|E2E3|all]
#
# Baseline reference: ae5a86b7 (May–Jul 2024, PF 1.0)
#
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${REPO}/.env.compose"
LOG_DIR="${REPO}/.run/exit_risk_experiments"
MODE="${1:-all}"
DATE_FROM="2024-05-01"
DATE_TO="2024-07-31"
mkdir -p "${LOG_DIR}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

log() { echo "[$(date -Is)] $*" | tee -a "${LOG_DIR}/master.log"; }

wait_hist() {
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
  sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
    build strategy_app_historical >> "${LOG_DIR}/build.log" 2>&1
  sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate --pull never strategy_app_historical >> "${LOG_DIR}/master.log" 2>&1
  sleep 20
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 240 || true
}

analyze() {
  local tag="$1" rid="$2"
  local log="${LOG_DIR}/${tag}.log"
  sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_run.py" option_trading-dashboard-1:/tmp/analyze_oos_validation_run.py
  sudo docker cp "${REPO}/ops/gcp/analyze_trade_forensics.py" option_trading-dashboard-1:/tmp/analyze_trade_forensics.py
  sudo docker cp "${REPO}/ops/gcp/monthly_forensics_breakdown.py" option_trading-dashboard-1:/tmp/monthly_forensics_breakdown.py 2>/dev/null || true
  sudo docker cp "${REPO}/ops/gcp/diagnose_oos_replay_coverage.py" option_trading-dashboard-1:/tmp/diagnose_oos_replay_coverage.py
  {
    echo ""
    echo "======== ${tag} run_id=${rid} ========"
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/diagnose_oos_replay_coverage.py "${rid}"
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/analyze_oos_validation_run.py "${rid}" "oos_${tag}"
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/monthly_forensics_breakdown.py "${rid}" 2>/dev/null || true
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/analyze_trade_forensics.py --run-id "${rid}" \
      --date-from "${DATE_FROM}" --date-to "${DATE_TO}" --top 10
  } >> "${log}" 2>&1
  echo "${tag}=${rid}" >> "${LOG_DIR}/run_ids.env"
}

run_one() {
  local tag="$1"
  local profile_patch="$2"
  local extra_risk="${3:-}"
  local log="${LOG_DIR}/${tag}.log"
  : > "${log}"
  log "START ${tag}"
  sudo bash "${REPO}/ops/gcp/clean_state_before_replay.sh" >> "${log}" 2>&1
  sudo bash "${profile_patch}" "${ENV_FILE}" >> "${log}" 2>&1
  sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" >> "${log}" 2>&1
  if [ -n "${extra_risk}" ]; then
    sudo bash "${extra_risk}" "${ENV_FILE}" >> "${log}" 2>&1
  fi
  wait_hist >> "${log}" 2>&1
  export REPLAY_EMIT_SNAPS_PER_MIN="${REPLAY_EMIT_SNAPS_PER_MIN:-2400}"
  "${PY}" "${REPO}/ops/gcp/preflight_historical_replay.py" >> "${log}" 2>&1 || true
  RID="$("${PY}" "${REPO}/ops/gcp/queue_replay.py" "${DATE_FROM}" "${DATE_TO}" | "${PY}" -c "
import json,sys
print(json.loads(sys.stdin.read()).get('run_id',''))
")"
  log "queued ${tag} run_id=${RID} emit_rate=${REPLAY_EMIT_SNAPS_PER_MIN}/min — waiting emission+drain"
  sudo docker cp "${REPO}/ops/gcp/wait_replay_closes.py" option_trading-dashboard-1:/tmp/wait_replay_closes.py
  if ! sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/wait_replay_closes.py "${RID}" --min-closes 400 --timeout-sec 5400 --stable-polls 8 \
    >> "${log}" 2>&1; then
    log "WARN: drain incomplete for ${RID} — analyzing anyway"
  fi
  analyze "${tag}" "${RID}"
  log "DONE ${tag} run_id=${RID}"
}

cd "${REPO}"
git pull --ff-only origin main 2>/dev/null || true

case "${MODE}" in
  E1) run_one "E1_stagnant_20" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_stagnant_20_env.sh" "" ;;
  E2) run_one "E2_dyn_exit" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dyn_exit_env.sh" "" ;;
  E3) run_one "E3_baseline_stress_risk" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" "${REPO}/ops/gcp/patch_eval_risk_stress_env.sh" ;;
  E2E3) run_one "E2E3_dyn_exit_stress" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dyn_exit_env.sh" "${REPO}/ops/gcp/patch_eval_risk_stress_env.sh" ;;
  E4) run_one "E4_stagnant20_dyn_exit" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_stagnant_20_dyn_exit_env.sh" "" ;;
  all)
    run_one "E1_stagnant_20" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_stagnant_20_env.sh" ""
    run_one "E2_dyn_exit" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dyn_exit_env.sh" ""
    run_one "E2E3_dyn_exit_stress" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dyn_exit_env.sh" "${REPO}/ops/gcp/patch_eval_risk_stress_env.sh"
    run_one "E4_stagnant20_dyn_exit" "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_stagnant_20_dyn_exit_env.sh" ""
    ;;
  *) echo "Usage: $0 [E1|E2|E2E3|E4|all]"; exit 2 ;;
esac

log "finished — logs in ${LOG_DIR}/"
echo ""
echo "=== Compare (grep PF / TIME_STOP / Jul) ==="
grep -hE 'Profit factor|TIME_STOP|2024-07|closes:|OVERALL' "${LOG_DIR}"/*.log 2>/dev/null | tail -80 || true

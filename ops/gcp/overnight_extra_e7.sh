#!/usr/bin/env bash
# Extra overnight E7 direction variants (sequential, after E7D5 finishes)
set -euo pipefail

REPO="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${REPO}/.env.compose"
LOG_DIR="${REPO}/.run/exit_risk_experiments"
DATE_FROM="2024-08-01"
DATE_TO="2024-10-31"
mkdir -p "${LOG_DIR}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

log() { echo "[$(date -Is)] $*" | tee -a "${LOG_DIR}/master.log"; }

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "${ENV_FILE}" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "${ENV_FILE}"
  else
    echo "${key}=${val}" >> "${ENV_FILE}"
  fi
}

patch_base_e7_env() {
  sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_e7_env.sh" "${ENV_FILE}"
  upsert ML_ENTRY_BLOCK_PE 0
  upsert ML_ENTRY_BLOCK_CE 0
  upsert ML_ENTRY_CE_ONLY 0
  upsert ML_ENTRY_PE_ONLY 0
}

patch_e7d3_no_ml_high_margin() {
  patch_base_e7_env
  upsert ML_ENTRY_DIRECTION_MODE consensus
  upsert DIRECTION_CONSENSUS_MIN_MARGIN 1.75
  upsert DIRECTION_CONSENSUS_RULE_WEIGHT 1.0
  upsert DIRECTION_CONSENSUS_SHADOW_WEIGHT 1.0
  upsert DIRECTION_CONSENSUS_MOMENTUM_WEIGHT 0.75
  upsert DIRECTION_CONSENSUS_ML_WEIGHT 0.0
  upsert DYNAMIC_SCRATCH_ENABLED 1
  upsert OPP_SIDE_PREM_SCRATCH_ENABLED 1
  upsert OPP_SIDE_PREM_DOM_RATIO 1.12
  upsert STAGNANT_PROFIT_EXIT_ENABLED 1
  upsert STAGNANT_PROFIT_PCT 0.03
  upsert STAGNANT_PROFIT_DECEL_BARS 2
  echo "Patched ${ENV_FILE} for e7d3_no_ml_high_margin"
}

patch_shadow_only() {
  patch_base_e7_env
  upsert ML_ENTRY_DIRECTION_MODE consensus
  upsert DIRECTION_CONSENSUS_MIN_MARGIN 0.75
  upsert DIRECTION_CONSENSUS_RULE_WEIGHT 0.0
  upsert DIRECTION_CONSENSUS_SHADOW_WEIGHT 2.0
  upsert DIRECTION_CONSENSUS_MOMENTUM_WEIGHT 0.0
  upsert DIRECTION_CONSENSUS_ML_WEIGHT 0.0
  upsert DYNAMIC_SCRATCH_ENABLED 1
  upsert OPP_SIDE_PREM_SCRATCH_ENABLED 1
  upsert OPP_SIDE_PREM_DOM_RATIO 1.12
  upsert STAGNANT_PROFIT_EXIT_ENABLED 1
  upsert STAGNANT_PROFIT_PCT 0.03
  upsert STAGNANT_PROFIT_DECEL_BARS 2
  echo "Patched ${ENV_FILE} for e7_shadow_only"
}

patch_momentum_only() {
  patch_base_e7_env
  upsert ML_ENTRY_DIRECTION_MODE consensus
  upsert DIRECTION_CONSENSUS_MIN_MARGIN 0.75
  upsert DIRECTION_CONSENSUS_RULE_WEIGHT 0.0
  upsert DIRECTION_CONSENSUS_SHADOW_WEIGHT 0.0
  upsert DIRECTION_CONSENSUS_MOMENTUM_WEIGHT 1.5
  upsert DIRECTION_CONSENSUS_ML_WEIGHT 0.0
  upsert DYNAMIC_SCRATCH_ENABLED 1
  upsert OPP_SIDE_PREM_SCRATCH_ENABLED 1
  upsert OPP_SIDE_PREM_DOM_RATIO 1.12
  upsert STAGNANT_PROFIT_EXIT_ENABLED 1
  upsert STAGNANT_PROFIT_PCT 0.03
  upsert STAGNANT_PROFIT_DECEL_BARS 2
  echo "Patched ${ENV_FILE} for e7_momentum_only"
}

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
  local tag="$1" patch_fn="$2" min_closes="${3:-40}"
  local log="${LOG_DIR}/${tag}.log"
  : > "${log}"
  log "START ${tag}"
  sudo bash "${REPO}/ops/gcp/clean_state_before_replay.sh" >> "${log}" 2>&1
  ${patch_fn} >> "${log}" 2>&1
  sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" >> "${log}" 2>&1
  wait_hist >> "${log}" 2>&1
  export REPLAY_EMIT_SNAPS_PER_MIN="${REPLAY_EMIT_SNAPS_PER_MIN:-2400}"
  "${PY}" "${REPO}/ops/gcp/preflight_historical_replay.py" >> "${log}" 2>&1 || true
  RID="$("${PY}" "${REPO}/ops/gcp/queue_replay.py" "${DATE_FROM}" "${DATE_TO}" | "${PY}" -c 'import json,sys;print(json.loads(sys.stdin.read()).get("run_id",""))')"
  log "queued ${tag} run_id=${RID} emit_rate=${REPLAY_EMIT_SNAPS_PER_MIN}/min min_closes=${min_closes} — waiting emission+drain"
  sudo docker cp "${REPO}/ops/gcp/wait_replay_closes.py" option_trading-dashboard-1:/tmp/wait_replay_closes.py
  if ! sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/wait_replay_closes.py "${RID}" --min-closes "${min_closes}" --timeout-sec 5400 --stable-polls 8 \
    >> "${log}" 2>&1; then
    log "WARN: drain incomplete for ${RID} — analyzing anyway"
  fi
  analyze "${tag}" "${RID}"
  log "DONE ${tag} run_id=${RID}"
}

wait_for_log_tag_done() {
  local tag="$1" max_sec="${2:-21600}" elapsed=0 step=20
  while true; do
    if grep -q "DONE ${tag} run_id=" "${LOG_DIR}/master.log" 2>/dev/null; then
      log "Detected completion of ${tag}"
      break
    fi
    sleep "${step}"
    elapsed=$((elapsed+step))
    if [ "${elapsed}" -ge "${max_sec}" ]; then
      log "TIMEOUT waiting for ${tag} completion"
      break
    fi
  done
}

main() {
  cd "${REPO}"
  git pull --ff-only origin main 2>/dev/null || true
  wait_for_log_tag_done "E7D5_consensus_high_margin_aug_oct" 21600
  run_one "E7D3_no_ml_high_margin_aug_oct" patch_e7d3_no_ml_high_margin 40
  run_one "E7_shadow_only_aug_oct" patch_shadow_only 40
  run_one "E7_momentum_only_aug_oct" patch_momentum_only 40
  log "overnight extra E7 runs finished"
}

main "$@"

#!/usr/bin/env bash
# Ops/GCP suite: E2-S6 in-sample + E2-S7 diagnose + E2-S8 parquet check.
# Optionally queue E3-S1 PE-only primary OOS (Engine flags via patch).
#
# Usage on VM:
#   sudo bash ops/gcp/run_ops_replay_suite.sh diagnose
#   sudo bash ops/gcp/run_ops_replay_suite.sh in_sample
#   sudo bash ops/gcp/run_ops_replay_suite.sh pe_only_primary
#   sudo bash ops/gcp/run_ops_replay_suite.sh all
#
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
MODE="${1:-all}"
LOG="${OPS_REPLAY_LOG:-/tmp/ops_replay_suite.log}"
DASH="${DASH_CONTAINER:-option_trading-dashboard-1}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

log() { echo "[$(date -Is)] $*" | tee -a "${LOG}"; }

copy_py_to_dash() {
  for f in analyze_oos_validation_run.py diagnose_oos_replay_coverage.py check_parquet_coverage.py analyze_direction_quality.py; do
    sudo docker cp "${REPO}/ops/gcp/${f}" "${DASH}:/tmp/${f}" 2>/dev/null || true
  done
}

run_diagnose() {
  log "E2-S8 parquet coverage"
  "${PY}" "${REPO}/ops/gcp/check_parquet_coverage.py" | tee -a "${LOG}"
  log "E2-S7 diagnose prior runs"
  copy_py_to_dash
  sudo docker exec "${DASH}" python /tmp/diagnose_oos_replay_coverage.py \
    57e60de8-0cde-4117-a4a8-da1a6fe3b79d \
    5104f59d-d922-4484-972a-27b42e2b75d9 \
    793f3a4d-a658-4e59-a552-2756180d9e0b | tee -a "${LOG}"
}

run_in_sample() {
  log "E2-S6 full in-sample replay"
  sudo bash "${REPO}/ops/gcp/run_oos_validation_replay.sh" in_sample_sanity 2>&1 | tee -a "${LOG}"
}

run_pe_only_primary() {
  log "E3-S1 PE-only primary OOS (Engine patch)"
  export ENTRY_ML_MIN_PROB=0.65
  sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_pe_only_env.sh" "${REPO}/.env.compose"
  cd "${REPO}"
  REPO_ROOT="${REPO}" "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate --pull never strategy_app_historical
  sleep 15
  REPO_ROOT="${REPO}" "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 180 || true
  sudo bash "${REPO}/ops/gcp/run_oos_validation_replay.sh" oos_primary 2>&1 | tee -a "${LOG}"
  log "Update docs/SCRUM_BOARD_ML_ENTRY_DIRECTION.md results log: oos_primary_pe_only"
}

case "${MODE}" in
  diagnose) run_diagnose ;;
  in_sample) run_in_sample ;;
  pe_only_primary) run_pe_only_primary ;;
  all)
    run_diagnose
    run_in_sample
    ;;
  *)
    echo "Usage: $0 {diagnose|in_sample|pe_only_primary|all}" >&2
    exit 2
    ;;
esac

log "ops suite done (${MODE})"

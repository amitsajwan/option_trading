#!/usr/bin/env bash
# Engine suite: E3-S1 baseline vs PE-only vs direction-ML on oos_primary.
#
# Prereq: direction bundle exists or set DIRECTION_ML_MODEL_PATH / RUN_DIR for export.
#
# Usage:
#   sudo bash ops/gcp/run_engine_direction_ab.sh baseline
#   sudo bash ops/gcp/run_engine_direction_ab.sh pe_only
#   sudo bash ops/gcp/run_engine_direction_ab.sh direction_ml       # E3-S2: det_dir_v1 + dir ML
#   sudo bash ops/gcp/run_engine_direction_ab.sh v1_direction_ml    # E3-S5: ML-only profile + dir ML
#   sudo bash ops/gcp/run_engine_direction_ab.sh v1_momentum        # E3-S3: same profile + momentum
#   sudo bash ops/gcp/run_engine_direction_ab.sh export_direction   # optional RUN_DIR=...
#
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
VARIANT="${1:-baseline}"
ENV_FILE="${REPO}/.env.compose"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

wait_consumers() {
  cd "${REPO}"
  # Restart does not reload .env.compose — recreate so STRATEGY_PROFILE_ID / direction path apply.
  REPO_ROOT="${REPO}" "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
  sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate --pull never strategy_app_historical
  sleep 20
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 180 || true
}

run_direction_quality_gate() {
  local variant="$1"
  case "${variant}" in
    direction_ml|v1_direction_ml|v1_momentum|v1_dual_direction_ml) ;;
    *) return 0 ;;
  esac
  local dash="${DASH_CONTAINER:-option_trading-dashboard-1}"
  sudo docker cp "${REPO}/ops/gcp/analyze_direction_quality.py" "${dash}:/tmp/analyze_direction_quality.py"
  local rid
  rid="$("${PY}" -c "
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical', timeout=15) as r:
    d = json.load(r)
run = d if d.get('run_id') else d.get('run') or d
print(run.get('run_id',''))
")"
  if [ -n "${rid}" ]; then
    echo "[$(date -Is)] direction quality analysis for ${rid}"
    sudo docker exec "${dash}" python /tmp/analyze_direction_quality.py "${rid}" "oos_primary_${variant}"
  fi
}

rebuild_hist() {
  cd "${REPO}"
  REPO_ROOT="${REPO}" "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
  sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
    build strategy_app_historical
  sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d --force-recreate --pull never strategy_app_historical
  sleep 15
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 180 || true
}

case "${VARIANT}" in
  baseline)
    export ENTRY_ML_MIN_PROB=0.65
    export ML_ENTRY_PE_ONLY=0
    export ML_ENTRY_BLOCK_CE=0
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh" "${ENV_FILE}"
    sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}"
    ;;
  pe_only)
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_pe_only_env.sh" "${ENV_FILE}"
    ;;
  direction_ml)
    if [ -n "${RUN_DIR:-}" ]; then
      "${PY}" -m ml_pipeline_2.scripts.export_direction_bundle_from_research \
        --run-dir "${RUN_DIR}" \
        --output-dir "${REPO}/ml_pipeline_2/artifacts/direction_only/published"
    fi
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_direction_ml_env.sh" "${ENV_FILE}"
    ;;
  v1_direction_ml)
    # E3-S5: clean ML-only profile (no rule strategies, no TRADER_SKIP / OI_UNWINDING veto).
    if [ -n "${RUN_DIR:-}" ]; then
      "${PY}" -m ml_pipeline_2.scripts.export_direction_bundle_from_research \
        --run-dir "${RUN_DIR}" \
        --output-dir "${REPO}/ml_pipeline_2/artifacts/direction_only/published"
    fi
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" "${ENV_FILE}"
    sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}"
    ;;
  v1_momentum)
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_momentum_env.sh" "${ENV_FILE}"
    sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}"
    ;;
  v1_dual_direction_ml)
    # E3-S6: ML-only profile + dual direction bundle (CE + PE per-side models).
    if [ -n "${CE_RUN_DIR:-}" ] && [ -n "${PE_RUN_DIR:-}" ]; then
      "${PY}" -m ml_pipeline_2.scripts.export_direction_dual_bundle \
        --ce-run-dir "${CE_RUN_DIR}" \
        --pe-run-dir "${PE_RUN_DIR}" \
        --output-dir "${REPO}/ml_pipeline_2/artifacts/direction_dual/published"
    fi
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dual_dir_env.sh" "${ENV_FILE}"
    sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}"
    ;;
  export_direction)
    RUN_DIR="${RUN_DIR:?Set RUN_DIR to completed direction_s2_only HPO run}"
    "${PY}" -m ml_pipeline_2.scripts.export_direction_bundle_from_research \
      --run-dir "${RUN_DIR}" \
      --output-dir "${REPO}/ml_pipeline_2/artifacts/direction_only/published"
    exit 0
    ;;
  replay_only)
  VARIANT="${2:-pe_only}"
  case "${VARIANT}" in
    baseline) export ENTRY_ML_MIN_PROB=0.65; export ML_ENTRY_PE_ONLY=0; export ML_ENTRY_BLOCK_CE=0
      sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh" "${ENV_FILE}"
      sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" ;;
    pe_only) sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_pe_only_env.sh" "${ENV_FILE}" ;;
    direction_ml) sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_direction_ml_env.sh" "${ENV_FILE}" ;;
    v1_direction_ml) sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" "${ENV_FILE}"
      sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" ;;
    v1_momentum) sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_momentum_env.sh" "${ENV_FILE}"
      sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" ;;
    v1_dual_direction_ml) sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dual_dir_env.sh" "${ENV_FILE}"
      sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" ;;
    *) echo "replay_only needs baseline|pe_only|direction_ml|v1_direction_ml|v1_momentum|v1_dual_direction_ml"; exit 2 ;;
  esac
  wait_consumers
  sudo OOS_REPLAY_SKIP_ENV_PATCH=1 bash "${REPO}/ops/gcp/run_oos_validation_replay.sh" replay_only oos_primary
  run_direction_quality_gate "${VARIANT}"
  exit 0
    ;;
  *)
    echo "Usage: $0 {baseline|pe_only|direction_ml|v1_direction_ml|v1_momentum|v1_dual_direction_ml|export_direction|replay_only [variant]}" >&2
    exit 2
    ;;
esac

rebuild_hist
sudo OOS_REPLAY_SKIP_ENV_PATCH=1 bash "${REPO}/ops/gcp/run_oos_validation_replay.sh" oos_primary
echo "Log run_id in SCRUM_BOARD results: oos_primary_${VARIANT}"
run_direction_quality_gate "${VARIANT}"

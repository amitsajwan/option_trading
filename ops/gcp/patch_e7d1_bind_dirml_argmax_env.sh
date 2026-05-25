#!/usr/bin/env bash
# E7D1: Keep best ENTRY; bind direction using direction-ML (argmax / dual_min_prob=0.0)
set -euo pipefail
ENV_FILE="${1:-/opt/option_trading/.env.compose}"
REPO="${REPO_ROOT:-/opt/option_trading}"

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_e7_env.sh" "$ENV_FILE"

# Allow both sides
upsert ML_ENTRY_BLOCK_PE 0
upsert ML_ENTRY_BLOCK_CE 0
upsert ML_ENTRY_CE_ONLY 0
upsert ML_ENTRY_PE_ONLY 0

# Direction: bind with dir-ML (argmax)
upsert ML_ENTRY_DIRECTION_MODE bind
upsert DIRECTION_ML_MODEL_PATH "${DIRECTION_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib}"
upsert DIRECTION_DUAL_MIN_PROB 0.0

# Exits
upsert DYNAMIC_SCRATCH_ENABLED 1
upsert OPP_SIDE_PREM_SCRATCH_ENABLED 1
upsert OPP_SIDE_PREM_DOM_RATIO 1.12
upsert STAGNANT_PROFIT_EXIT_ENABLED 1
upsert STAGNANT_PROFIT_PCT 0.03
upsert STAGNANT_PROFIT_DECEL_BARS 2

echo "Patched $ENV_FILE for E7D1 bind_dirml_argmax"

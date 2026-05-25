#!/usr/bin/env bash
# E7D0: Keep best ENTRY; bind direction using momentum fallback (no direction-ML).
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

# Start from E7 base (time windows and entry model)
sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_e7_env.sh" "$ENV_FILE"

# Allow both sides for direction test
upsert ML_ENTRY_BLOCK_PE 0
upsert ML_ENTRY_BLOCK_CE 0
upsert ML_ENTRY_CE_ONLY 0
upsert ML_ENTRY_PE_ONLY 0

# Direction: bind, but disable direction ML by clearing model path → momentum fallback
upsert ML_ENTRY_DIRECTION_MODE bind
upsert DIRECTION_ML_MODEL_PATH ""

# Exits (stabilizers)
upsert DYNAMIC_SCRATCH_ENABLED 1
upsert OPP_SIDE_PREM_SCRATCH_ENABLED 1
upsert OPP_SIDE_PREM_DOM_RATIO 1.12
upsert STAGNANT_PROFIT_EXIT_ENABLED 1
upsert STAGNANT_PROFIT_PCT 0.03
upsert STAGNANT_PROFIT_DECEL_BARS 2

echo "Patched $ENV_FILE for E7D0 bind_momentum"

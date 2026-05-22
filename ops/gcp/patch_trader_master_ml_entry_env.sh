#!/usr/bin/env bash
# Patch .env.compose for trader_master_ml_entry_v1 experiment (ML entry + multi-strategy exits).
set -euo pipefail

ENV_FILE="${1:-/opt/option_trading/.env.compose}"
ENTRY_MODEL="${ENTRY_ML_MODEL_PATH:-/opt/option_trading/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib}"

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

upsert STRATEGY_PROFILE_ID trader_master_ml_entry_v1
upsert ENTRY_ML_MODEL_PATH "$ENTRY_MODEL"
upsert ENTRY_ML_MIN_PROB "${ENTRY_ML_MIN_PROB:-0.55}"
# Optional: set DIRECTION_ML_MODEL_PATH for CE/PE when entry fires

echo "Patched $ENV_FILE for trader_master_ml_entry_v1"
grep -E '^(STRATEGY_PROFILE_ID|ENTRY_ML_)=' "$ENV_FILE" || true

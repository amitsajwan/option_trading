#!/usr/bin/env bash
# ML entry timing + deterministic rule direction (trader_master_ml_entry_det_dir_v1).
set -euo pipefail

ENV_FILE="${1:-/opt/option_trading/.env.compose}"
ENTRY_MODEL="${ENTRY_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib}"

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

upsert STRATEGY_ENGINE deterministic
upsert STRATEGY_PROFILE_ID trader_master_ml_entry_det_dir_v1
upsert STRATEGY_MIN_CONFIDENCE "${STRATEGY_MIN_CONFIDENCE:-0.50}"
upsert STRATEGY_ROLLOUT_STAGE_HISTORICAL paper
upsert STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL 1.0
upsert MARKET_SESSION_ENABLED 0
upsert ENTRY_ML_MODEL_PATH "$ENTRY_MODEL"
upsert ENTRY_ML_MIN_PROB "${ENTRY_ML_MIN_PROB:-0.55}"
upsert DIRECTION_ML_MODEL_PATH ""
upsert ML_PURE_RUN_ID ""
upsert ML_PURE_MODEL_GROUP ""
upsert ML_PURE_MODEL_PACKAGE ""
upsert ML_PURE_THRESHOLD_REPORT ""

echo "Patched $ENV_FILE for trader_master_ml_entry_det_dir_v1 (entry ML, direction rules)"
grep -E '^(STRATEGY_PROFILE_ID|ENTRY_ML_|DIRECTION_ML_)=' "$ENV_FILE" || true

#!/usr/bin/env bash
# Gate 1 (CE-only ship validation): same as trader_master_ml_entry_v1_dyn_exit
# but PE leg blocked. Multiple replays (Ref, E2, E4) show PE PF 0.79-0.89 vs
# CE PF 1.36-1.42. CE-only is the highest-leverage single change before live.
# Reuse for Aug-Oct 2024 OOS walk-forward (see HANDOVER_2026-05-22.md Gate 1).
set -euo pipefail

ENV_FILE="${1:-/opt/option_trading/.env.compose}"
ENTRY_MODEL="${ENTRY_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib}"
DIR_MODEL="${DIRECTION_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib}"

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

upsert STRATEGY_ENGINE deterministic
upsert STRATEGY_PROFILE_ID trader_master_ml_entry_v1_dyn_exit
upsert STRATEGY_MIN_CONFIDENCE "${STRATEGY_MIN_CONFIDENCE:-0.50}"
upsert STRATEGY_ROLLOUT_STAGE_HISTORICAL paper
upsert STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL 1.0
upsert MARKET_SESSION_ENABLED 0
upsert ENTRY_ML_MODEL_PATH "$ENTRY_MODEL"
upsert ENTRY_ML_MIN_PROB "${ENTRY_ML_MIN_PROB:-0.65}"
upsert ML_ENTRY_PE_ONLY 0
upsert ML_ENTRY_CE_ONLY 0
upsert ML_ENTRY_BLOCK_CE 0
upsert ML_ENTRY_BLOCK_PE 1
upsert DIRECTION_ML_MODEL_PATH "$DIR_MODEL"
upsert ML_PURE_RUN_ID ""
upsert ML_PURE_MODEL_GROUP ""
upsert ML_PURE_MODEL_PACKAGE ""
upsert ML_PURE_THRESHOLD_REPORT ""

echo "Patched $ENV_FILE for trader_master_ml_entry_v1_dyn_exit + ML_ENTRY_BLOCK_PE=1 (CE-only)"
grep -E '^(STRATEGY_PROFILE_ID|ENTRY_ML_|ML_ENTRY_|DIRECTION_ML_)=' "$ENV_FILE" || true

#!/usr/bin/env bash
# E7: CE-only × top-3 time windows. NO regime gate.
# This is E8 minus the daily regime tagger. If E7 also fails OOS, the
# long-ATM-1-min lane has no demonstrable edge — see memory:
# project_e8_oos_failure_2026-05-25 + project_rules_verdict.
#
# Run: sudo bash ops/gcp/run_exit_risk_experiments.sh E7
#      sudo bash ops/gcp/run_exit_risk_experiments.sh E7_aug_oct
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

# CE-only
upsert ML_ENTRY_PE_ONLY 0
upsert ML_ENTRY_CE_ONLY 0
upsert ML_ENTRY_BLOCK_CE 0
upsert ML_ENTRY_BLOCK_PE 1

# Top-3 time windows (entry-side filter only)
upsert ENTRY_TIME_WINDOWS "09:45-10:15,10:45-11:15,11:15-11:45"

# NO regime gate — clear leftovers from E8 / prior runs
upsert ENTRY_REGIME_TAGGER ""
upsert ENTRY_REGIME_ALLOWED_TAGS ""

# Match E8: bind direction (don't fall back to CE for blocked PE)
upsert ML_ENTRY_DIRECTION_MODE bind

upsert DIRECTION_ML_MODEL_PATH "$DIR_MODEL"
upsert ML_PURE_RUN_ID ""
upsert ML_PURE_MODEL_GROUP ""
upsert ML_PURE_MODEL_PACKAGE ""
upsert ML_PURE_THRESHOLD_REPORT ""

echo "Patched $ENV_FILE for E7 (CE-only × top-3 windows; NO regime gate)"
grep -E '^(STRATEGY_PROFILE_ID|ENTRY_ML_|ML_ENTRY_|ENTRY_TIME_WINDOWS|ENTRY_REGIME_)' "$ENV_FILE" || true

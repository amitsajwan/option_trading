#!/usr/bin/env bash
# ML_ENTRY timing + direction consensus (rules/shadow/momentum; ML dir = weak vote).
# ATM-only strikes; fast thesis-fail exit for 5m entry model.
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

ENTRY_MODEL="${ENTRY_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib}"
DIR_MODEL="${DIRECTION_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib}"

sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "$ENV_FILE"

upsert STRATEGY_PROFILE_ID "trader_master_ml_entry_consensus_v1"
upsert ENTRY_ML_MODEL_PATH "$ENTRY_MODEL"
upsert ENTRY_ML_MIN_PROB "${ENTRY_ML_MIN_PROB:-0.65}"
upsert DIRECTION_ML_MODEL_PATH "$DIR_MODEL"
upsert ML_ENTRY_DIRECTION_MODE "consensus"
upsert DIRECTION_CONSENSUS_MIN_MARGIN "${DIRECTION_CONSENSUS_MIN_MARGIN:-1.25}"
upsert DIRECTION_CONSENSUS_ML_WEIGHT "${DIRECTION_CONSENSUS_ML_WEIGHT:-0.35}"
upsert DIRECTION_CONSENSUS_RULE_WEIGHT "${DIRECTION_CONSENSUS_RULE_WEIGHT:-1.0}"
upsert DIRECTION_CONSENSUS_SHADOW_WEIGHT "${DIRECTION_CONSENSUS_SHADOW_WEIGHT:-1.0}"
upsert DIRECTION_CONSENSUS_MOMENTUM_WEIGHT "${DIRECTION_CONSENSUS_MOMENTUM_WEIGHT:-0.75}"
upsert STRATEGY_STRIKE_SELECTION_POLICY "atm"
upsert STRATEGY_STRIKE_MAX_OTM_STEPS "0"
upsert ML_ENTRY_DET_SKIP_BRAIN_GATE "true"

echo "Patched $ENV_FILE for trader_master_ml_entry_consensus_v1"
grep -E '^(STRATEGY_PROFILE_ID|ENTRY_ML_|DIRECTION_ML_|ML_ENTRY_DIRECTION|DIRECTION_CONSENSUS_|STRIKE_)=' "$ENV_FILE" || true

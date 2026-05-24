#!/usr/bin/env bash
# E3-S2: Wire direction_only bundle for ML_ENTRY side selection.
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${1:-${REPO}/.env.compose}"
DIR_MODEL="${DIRECTION_ML_MODEL_PATH:-/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib}"

export ENTRY_ML_MIN_PROB="${ENTRY_ML_MIN_PROB:-0.65}"
export ML_ENTRY_PE_ONLY=0
export ML_ENTRY_BLOCK_CE=0

bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh" "$ENV_FILE"
bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "$ENV_FILE"

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

upsert DIRECTION_ML_MODEL_PATH "$DIR_MODEL"
upsert ML_ENTRY_PE_ONLY 0
upsert ML_ENTRY_BLOCK_CE 0

echo "Patched direction ML for ML_ENTRY: $DIR_MODEL"
grep -E '^(DIRECTION_ML_MODEL_PATH|ML_ENTRY_PE_ONLY|ENTRY_ML_MIN_PROB)=' "$ENV_FILE" || true

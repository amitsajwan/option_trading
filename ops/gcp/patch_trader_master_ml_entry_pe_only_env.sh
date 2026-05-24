#!/usr/bin/env bash
# E3-S1 A/B: ML_ENTRY takes PE only (momentum CE path disabled).
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${1:-${REPO}/.env.compose}"

export ENTRY_ML_MIN_PROB="${ENTRY_ML_MIN_PROB:-0.65}"
export ML_ENTRY_PE_ONLY=1
export ML_ENTRY_BLOCK_CE=0
unset DIRECTION_ML_MODEL_PATH 2>/dev/null || true

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

upsert ML_ENTRY_PE_ONLY 1
upsert ML_ENTRY_BLOCK_CE 0
upsert DIRECTION_ML_MODEL_PATH ""

echo "Patched PE-only ML_ENTRY for eval replay"
grep -E '^(ML_ENTRY_PE_ONLY|ML_ENTRY_BLOCK_CE|ENTRY_ML_MIN_PROB)=' "$ENV_FILE" || true

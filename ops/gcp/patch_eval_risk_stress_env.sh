#!/usr/bin/env bash
# E3: Tighter drawdown control for eval (Jul stress spirals).
# Apply on top of patch_trader_master_eval_replay_env.sh.
set -euo pipefail

ENV_FILE="${1:-/opt/option_trading/.env.compose}"

upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

upsert RISK_MAX_CONSECUTIVE_LOSSES "${RISK_MAX_CONSECUTIVE_LOSSES:-4}"
upsert RISK_MAX_SESSION_TRADES "${RISK_MAX_SESSION_TRADES:-12}"
upsert RISK_MAX_DAILY_LOSS_PCT "${RISK_MAX_DAILY_LOSS_PCT:-0.03}"

echo "Patched $ENV_FILE — stress risk (consec=${RISK_MAX_CONSECUTIVE_LOSSES:-4} daily_loss=${RISK_MAX_DAILY_LOSS_PCT:-0.03})"
grep -E '^(RISK_MAX_CONSECUTIVE_LOSSES|RISK_MAX_DAILY_LOSS_PCT)=' "$ENV_FILE" || true

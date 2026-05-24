#!/usr/bin/env bash
# Relax risk limits for historical eval replays (measure edge, not live risk choke).
# Use with trader_master_ml_entry_det_dir_v1 OOS runs only — not for production live.
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

upsert RISK_MAX_CONSECUTIVE_LOSSES "${RISK_MAX_CONSECUTIVE_LOSSES:-15}"
upsert RISK_MAX_SESSION_TRADES "${RISK_MAX_SESSION_TRADES:-12}"
# Keep brain on (breakthrough run used brain); only relax session risk for replay fidelity.
upsert BRAIN_ENABLED "${BRAIN_ENABLED:-true}"
upsert BRAIN_CONSENSUS_MIN_AGREEING "${BRAIN_CONSENSUS_MIN_AGREEING:-1}"
upsert ML_ENTRY_DET_SKIP_BRAIN_GATE "${ML_ENTRY_DET_SKIP_BRAIN_GATE:-true}"

echo "Patched $ENV_FILE for eval replay (consec=${RISK_MAX_CONSECUTIVE_LOSSES:-15} session_trades=${RISK_MAX_SESSION_TRADES:-12})"
grep -E '^(RISK_MAX_CONSECUTIVE_LOSSES|RISK_MAX_SESSION_TRADES|BRAIN_ENABLED)=' "$ENV_FILE" || true

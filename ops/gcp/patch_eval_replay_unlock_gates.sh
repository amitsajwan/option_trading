#!/usr/bin/env bash
# Eval replay: remove session trade cap choke (forensics: top miss blocker).
# Use only for historical measurement — not production live.
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

# 0 = disabled (RiskManager only enforces when > 0)
upsert RISK_MAX_SESSION_TRADES "${RISK_MAX_SESSION_TRADES:-0}"
upsert RISK_MAX_CONSECUTIVE_LOSSES "${RISK_MAX_CONSECUTIVE_LOSSES:-20}"
upsert BRAIN_ENABLED "${BRAIN_ENABLED:-true}"
upsert BRAIN_CONSENSUS_MIN_AGREEING "${BRAIN_CONSENSUS_MIN_AGREEING:-1}"
upsert ML_ENTRY_DET_SKIP_BRAIN_GATE "${ML_ENTRY_DET_SKIP_BRAIN_GATE:-true}"

echo "Patched $ENV_FILE — unlock gates (session_trades=${RISK_MAX_SESSION_TRADES:-0})"
grep -E '^(RISK_MAX_SESSION_TRADES|RISK_MAX_CONSECUTIVE_LOSSES)=' "$ENV_FILE" || true

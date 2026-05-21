#!/bin/bash
set -euo pipefail
ENV="${1:-/opt/option_trading/.env.compose}"
touch "$ENV"
set_kv() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV"
  else
    echo "${key}=${val}" >>"$ENV"
  fi
}
set_kv STRATEGY_ENGINE deterministic
set_kv STRATEGY_PROFILE_ID trader_master_v1
set_kv STRATEGY_MIN_CONFIDENCE 0.50
set_kv STRATEGY_ROLLOUT_STAGE_HISTORICAL paper
set_kv STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL 1.0
set_kv MARKET_SESSION_ENABLED 0
set_kv BRAIN_DAILY_FEATURES_PATH /app/.data/ml_pipeline/daily_regime_features.json
set_kv BRAIN_ENABLED true
echo "patched $ENV (trader_master_v1)"
grep -E '^STRATEGY_ENGINE=|^STRATEGY_PROFILE_ID=|^BRAIN_' "$ENV"

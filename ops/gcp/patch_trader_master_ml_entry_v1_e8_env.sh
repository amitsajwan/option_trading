#!/usr/bin/env bash
# E8: CE-only × NOT-bull regime × top-3 time windows.
# Stacks on E2's dyn_exit profile. Counterfactual on Ref+E2 showed net PF 2.49-2.78
# with bootstrap lower bound > 1.0 — first config in the research arc to clear that
# threshold. See memory: project_e8_regime_finding_2026-05-25.
#
# Triple filter applied at entry time:
#  1. PE leg blocked (ML_ENTRY_BLOCK_PE=1) — CE-only baseline already had PF 1.17-1.22
#  2. Daily regime tag must NOT be 'bull' (combined_majority tagger)
#  3. Entries only inside top-3 IST windows (09:45-10:15, 10:45-11:15, 11:15-11:45)
#
# Run: sudo bash ops/gcp/run_exit_risk_experiments.sh E8        # May-Jul sanity
#      sudo bash ops/gcp/run_exit_risk_experiments.sh E8_aug_oct # OOS gate (the real test)
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

# Inherit E2 dyn_exit profile + exits (best baseline)
upsert STRATEGY_ENGINE deterministic
upsert STRATEGY_PROFILE_ID trader_master_ml_entry_v1_dyn_exit
upsert STRATEGY_MIN_CONFIDENCE "${STRATEGY_MIN_CONFIDENCE:-0.50}"
upsert STRATEGY_ROLLOUT_STAGE_HISTORICAL paper
upsert STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL 1.0
upsert MARKET_SESSION_ENABLED 0
upsert ENTRY_ML_MODEL_PATH "$ENTRY_MODEL"
upsert ENTRY_ML_MIN_PROB "${ENTRY_ML_MIN_PROB:-0.65}"

# Layer 1: CE-only
upsert ML_ENTRY_PE_ONLY 0
upsert ML_ENTRY_CE_ONLY 0
upsert ML_ENTRY_BLOCK_CE 0
upsert ML_ENTRY_BLOCK_PE 1

# Layer 2: daily regime gate (combined_majority tagger; only trade bear+chop days)
upsert ENTRY_REGIME_TAGGER combined_majority
upsert ENTRY_REGIME_ALLOWED_TAGS "bear,chop"

# Layer 3: entry time-window filter (top-3 IST windows from E7 decomposition)
upsert ENTRY_TIME_WINDOWS "09:45-10:15,10:45-11:15,11:15-11:45"

# Direction ML still loaded but its vote is no-op since CE is forced
upsert DIRECTION_ML_MODEL_PATH "$DIR_MODEL"
upsert ML_PURE_RUN_ID ""
upsert ML_PURE_MODEL_GROUP ""
upsert ML_PURE_MODEL_PACKAGE ""
upsert ML_PURE_THRESHOLD_REPORT ""

echo "Patched $ENV_FILE for E8 (CE-only × NOT-bull × top-3 windows)"
grep -E '^(STRATEGY_PROFILE_ID|ENTRY_ML_|ML_ENTRY_|ENTRY_TIME_WINDOWS|ENTRY_REGIME_)' "$ENV_FILE" || true

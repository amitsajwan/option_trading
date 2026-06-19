#!/usr/bin/env bash
# ops/deploy_compression_v1.sh
# Deploy compression_v1 entry model to runtime VM containers.
# Run from the runtime VM: bash /opt/option_trading/ops/deploy_compression_v1.sh
#
# Prerequisites:
#   1. bundle published to GCS: gs://amit-trading-option-trading-models/published_models/entry_compression_v1/
#   2. git pull already done (or run this script after pulling)
set -euo pipefail

REPO=/opt/option_trading
ENV_FILE="$REPO/.env.compose"
MODEL_GCS="gs://amit-trading-option-trading-models/published_models/entry_compression_v1/entry_compression_v1.joblib"
MODEL_LOCAL="/tmp/entry_compression_v1.joblib"
MODEL_CONTAINER="/app/ml_pipeline_2/artifacts/entry_only/published/entry_compression_v1.joblib"

echo "=== [1/6] git pull ==="
cd "$REPO" && git pull origin feat/compression-state-engine

echo ""
echo "=== [2/6] Download bundle from GCS ==="
gsutil cp "$MODEL_GCS" "$MODEL_LOCAL"
echo "  downloaded -> $MODEL_LOCAL"

echo ""
echo "=== [3/6] Copy bundle into containers ==="
for CONTAINER in strategy_app strategy_app_sim; do
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        # ensure target dir exists
        docker exec "$CONTAINER" mkdir -p "$(dirname $MODEL_CONTAINER)"
        docker cp "$MODEL_LOCAL" "${CONTAINER}:${MODEL_CONTAINER}"
        echo "  ✓ copied to $CONTAINER"
    else
        echo "  ⚠ $CONTAINER not running — skipping"
    fi
done

echo ""
echo "=== [4/6] Update .env.compose ==="
set_kv() {
    local KEY="$1" VAL="$2"
    if grep -q "^${KEY}=" "$ENV_FILE"; then
        sed -i "s|^${KEY}=.*|${KEY}=${VAL}|" "$ENV_FILE"
        echo "  updated  $KEY=$VAL"
    else
        echo "" >> "$ENV_FILE"
        echo "${KEY}=${VAL}" >> "$ENV_FILE"
        echo "  added    $KEY=$VAL"
    fi
}

# New model path + threshold (recommended_min_prob from calibration report)
set_kv "ENTRY_ML_MODEL_PATH"       "$MODEL_CONTAINER"
set_kv "ENTRY_ML_MIN_PROB"         "0.50"

# Compression model fires from 9:35 — open the full session
set_kv "ENTRY_TIME_WINDOWS"        "09:35-15:00"

# Opportunity gate — 3 best bars/day ranked by ML prob x ATR
set_kv "OPPORTUNITY_GATE_ENABLED"       "1"
set_kv "OPP_GATE_MAX_ENTRIES"           "3"
set_kv "OPP_GATE_SELECTION_MODE"        "percentile"
set_kv "OPP_GATE_SELECTION_PERCENTILE"  "75"
set_kv "OPP_GATE_MIN_SPACING_MINUTES"   "20"

# Direction: multi_signal mode — stateless 6-signal scorer, abstains when weak
# ENTRY_MULTI_SIGNAL_MIN=2.0: need at least 2 signals agreeing (score ≥ 2.0)
# ENTRY_SHADOW_SCORE_MIN=2.0: common-path gate — skip bars below this conviction
set_kv "ML_ENTRY_DIRECTION_MODE"          "multi_signal"
set_kv "ENTRY_MULTI_SIGNAL_MIN"           "2.0"
set_kv "ENTRY_SHADOW_SCORE_MIN"           "2.0"
set_kv "BRAIN_DUAL_MODE"                  "live"

# Sideways gate
set_kv "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED" "0"

echo ""
echo "=== [5/6] Restart strategy_app ==="
cd "$REPO" && docker compose --env-file .env.compose restart strategy_app
echo "  ✓ strategy_app restarted"

echo ""
echo "=== [6/6] Verify settings + model in container ==="
echo "--- env ---"
docker exec strategy_app env | grep -E "ENTRY_ML|ENTRY_TIME|OPPORTUNITY|OPP_GATE|SIDEWAYS"
echo "--- model file ---"
docker exec strategy_app ls -lh "$MODEL_CONTAINER" && echo "  ✓ model file present"
echo "--- compression features check ---"
docker exec strategy_app python3 -c "
import joblib, sys
b = joblib.load('$MODEL_CONTAINER')
feats = b.get('features', [])
comp = [f for f in feats if any(k in f for k in ['bb_width','compression','ema_spread','range_ratio','ema_order'])]
vel  = [f for f in feats if f.startswith('vel_') or f.startswith('ctx_am_') or f.startswith('ctx_gap_')]
print(f'  total features: {len(feats)}')
print(f'  compression features: {len(comp)} {comp[:3]}...')
print(f'  velocity (11:30) features: {len(vel)} <- should be 0')
sys.exit(0 if len(vel)==0 else 1)
" && echo "  ✓ serve-parity OK: no 11:30-anchored velocity features"

echo ""
echo "=== Deploy complete. Run SIM to verify entries fire from 09:35 ==="
echo "  curl -X POST http://localhost:8080/api/sim/run -H 'Content-Type: application/json' \\"
echo "       -d '{\"trade_date\": \"$(date +%Y-%m-%d)\"}'"

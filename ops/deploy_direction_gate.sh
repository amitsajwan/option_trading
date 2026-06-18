#!/usr/bin/env bash
# ops/deploy_direction_gate.sh
# Deploy direction signals (19-23) + opportunity gate to VM containers.
# Run from the VM: bash /opt/option_trading/ops/deploy_direction_gate.sh
set -euo pipefail

REPO=/opt/option_trading
ENGINE_SRC="$REPO/strategy_app/engines/deterministic_rule_engine.py"
OPP_SRC="$REPO/strategy_app/engines/opportunity.py"
ENV_FILE="$REPO/.env.compose"

echo "=== [1/5] git pull ==="
cd "$REPO" && git pull origin feat/compression-state-engine

echo ""
echo "=== [2/5] Deploy engine + opportunity gate to containers ==="
for CONTAINER in strategy_app strategy_app_sim; do
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        docker cp "$ENGINE_SRC" "${CONTAINER}:/app/strategy_app/engines/deterministic_rule_engine.py"
        docker cp "$OPP_SRC"    "${CONTAINER}:/app/strategy_app/engines/opportunity.py"
        echo "  ✓ Copied to $CONTAINER"
    else
        echo "  ⚠ $CONTAINER not running — skipping"
    fi
done

echo ""
echo "=== [3/5] Update .env.compose gate settings ==="

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

# Block pre-11:30 entries (model has 49/54 NaN velocity features before 11:30)
set_kv "ENTRY_TIME_WINDOWS"              "11:30-15:00"

# Enable opportunity gate — selects best 3 bars/day (ranked by ML prob × ATR)
set_kv "OPPORTUNITY_GATE_ENABLED"        "1"
set_kv "OPP_GATE_MAX_ENTRIES"            "3"
set_kv "OPP_GATE_SELECTION_MODE"         "percentile"
set_kv "OPP_GATE_SELECTION_PERCENTILE"   "75"
set_kv "OPP_GATE_MIN_SPACING_MINUTES"    "20"

# Keep sideways gate disabled (from previous fix)
set_kv "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED" "0"

echo ""
echo "=== [4/5] Restart strategy_app (picks up new .env.compose) ==="
cd "$REPO" && docker compose --env-file .env.compose restart strategy_app
echo "  ✓ strategy_app restarted"

echo ""
echo "=== [5/5] Verify settings inside container ==="
echo "--- ENTRY_TIME_WINDOWS ---"
docker exec strategy_app env | grep -E "ENTRY_TIME_WINDOWS|OPPORTUNITY_GATE|OPP_GATE|SIDEWAYS_RETURNS_MIXED"

echo ""
echo "=== Done. Next: run SIM on today's date ==="
echo "  Use the dashboard SIM button, or:"
echo "  curl -X POST http://localhost:8080/api/sim/run -H 'Content-Type: application/json' \\"
echo "       -d '{\"trade_date\": \"$(date +%Y-%m-%d)\"}'"

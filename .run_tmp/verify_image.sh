#!/bin/bash
# Check if the built strategy_app_sim image contains the confidence scaling fix
echo "=== Image info ==="
sudo docker images option_trading-strategy_app_sim --format '{{.Repository}} {{.Tag}} {{.CreatedAt}}' | head -5

echo ""
echo "=== ml_entry.py confidence line ==="
sudo docker run --rm option_trading-strategy_app_sim:latest grep -n 'confidence=' /app/strategy_app/engines/strategies/ml_entry.py | head -5

echo ""
echo "=== Check for scaling formula ==="
sudo docker run --rm option_trading-strategy_app_sim:latest grep -c '0.65 + 0.35' /app/strategy_app/engines/strategies/ml_entry.py

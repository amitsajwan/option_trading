#!/bin/bash
RUN_ID="02cd304b-db1b-438b-a601-c645f8902b35"
for i in {1..30}; do
  if [ -f "/opt/option_trading/.run/strategy_app_sim/$RUN_ID/result.json" ]; then
    echo "COMPLETED"
    exit 0
  fi
  if [ -f "/opt/option_trading/.run/strategy_app_sim/$RUN_ID/cancellation.json" ]; then
    echo "CANCELLED"
    exit 1
  fi
  sleep 10
done
echo "TIMEOUT"

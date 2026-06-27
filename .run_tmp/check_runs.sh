#!/bin/bash
for d in /opt/option_trading/.run/strategy_app_sim/*; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  echo "=== $name ==="
  ls -la "$d"/*.json 2>/dev/null || echo "  no json files"
done

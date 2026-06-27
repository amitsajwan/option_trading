#!/bin/bash
for d in /opt/option_trading/.run/strategy_app_sim/a7729382* \
         /opt/option_trading/.run/strategy_app_sim/90398e41* \
         /opt/option_trading/.run/strategy_app_sim/c323b149*; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  echo "=== $name ==="
  ls -la "$d"/*.json 2>/dev/null || echo "  no json files"
done

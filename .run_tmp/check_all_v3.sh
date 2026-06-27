#!/bin/bash
for d in /opt/option_trading/.run/strategy_app_sim/a7729382* \
         /opt/option_trading/.run/strategy_app_sim/90398e41* \
         /opt/option_trading/.run/strategy_app_sim/c323b149* \
         /opt/option_trading/.run/strategy_app_sim/32fca5d4* \
         /opt/option_trading/.run/strategy_app_sim/05cd2f2f* \
         /opt/option_trading/.run/strategy_app_sim/de13bfcd* \
         /opt/option_trading/.run/strategy_app_sim/a49914b2* \
         /opt/option_trading/.run/strategy_app_sim/ba77702e* \
         /opt/option_trading/.run/strategy_app_sim/8546e2e9*; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  if [ -f "$d/result.json" ]; then
    echo "COMPLETED $name"
  elif [ -f "$d/cancellation.json" ]; then
    echo "CANCELLED $name"
  else
    echo "RUNNING   $name"
  fi
done

#!/bin/bash
echo "=== ENTRY_ML / ENTRY_VOL_GATE in strategy_config.yml ==="
grep -n "ENTRY_ML\|ENTRY_VOL_GATE\|ATR_ENTRY" /opt/option_trading/ops/strategy_config.yml || echo "not found"
echo ""
echo "=== Current ops_env.json (key fields) ==="
python3 -c "
import json
d = json.load(open('/opt/option_trading/.run/strategy_app/ops_env.json'))
for k in ['ENTRY_ML_MODEL_PATH','ENTRY_ML_MIN_PROB','ENTRY_VOL_GATE_ENABLED','ATR_ENTRY_MIN_PCT']:
    print(f'  {k}: {d.get(k,\"NOT SET\")}')
"

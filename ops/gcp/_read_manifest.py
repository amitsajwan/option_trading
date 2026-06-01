#!/usr/bin/env python3
import json
import sys
from pathlib import Path

rid = sys.argv[1]
p = Path("/opt/option_trading/.run/strategy_app_sim") / rid / "manifest.json"
print(json.dumps(json.loads(p.read_text()), indent=2))

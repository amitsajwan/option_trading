#!/usr/bin/env python3
import json
import sys
import urllib.parse
import urllib.request

params = urllib.parse.urlencode({
    "strategy": sys.argv[1] if len(sys.argv) > 1 else "R1S_TOP3_SHORT_CE",
    "date_from": sys.argv[2] if len(sys.argv) > 2 else "2024-05-01",
    "date_to": sys.argv[3] if len(sys.argv) > 3 else "2024-07-31",
})
url = f"http://127.0.0.1:8008/api/strategy/evaluation/trades?{params}"
with urllib.request.urlopen(url, timeout=60) as resp:
    data = json.loads(resp.read().decode())
trades = data.get("trades", data) if isinstance(data, dict) else data
print(f"strategy={sys.argv[1] if len(sys.argv)>1 else 'R1S_TOP3_SHORT_CE'} count={len(trades)}")
if isinstance(data, dict) and "summary" in data:
    print(json.dumps(data["summary"], indent=2))

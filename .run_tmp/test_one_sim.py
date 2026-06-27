import json, urllib.request
payload = {
    "source_date": "2026-06-01",
    "source_coll": "phase1_market_snapshots",
    "label": "test_ml_confidence_fix",
    "speed": 30.0,
    "env_overrides": {},
}
req = urllib.request.Request(
    "http://localhost:8008/api/sim/runs",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(json.loads(resp.read()))
except Exception as e:
    print(f"ERROR: {e}")

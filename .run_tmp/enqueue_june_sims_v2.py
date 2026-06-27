"""Enqueue SIM runs for June 2026 dates via the dashboard API (v2)."""
import json, sys, urllib.request

DATES = [
    "2026-06-01", "2026-06-02", "2026-06-03",
    "2026-06-10", "2026-06-11", "2026-06-12", "2026-06-15",
    "2026-06-16", "2026-06-17",
]

BASE = "http://localhost:8008"

def enqueue(date: str) -> dict:
    payload = {
        "source_date": date,
        "source_coll": "phase1_market_snapshots",
        "label": f"june_ml_v2_{date}",
        "speed": 30.0,
        "env_overrides": {},
    }
    req = urllib.request.Request(
        f"{BASE}/api/sim/runs",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e), "date": date}

print(f"Enqueuing {len(DATES)} sim runs for June 2026 dates (v2)...")
for d in DATES:
    result = enqueue(d)
    if "error" in result:
        print(f"  FAIL {d}: {result['error']}")
    else:
        print(f"  OK   {d}: run_id={result.get('run_id', 'N/A')}")

print("Done.")

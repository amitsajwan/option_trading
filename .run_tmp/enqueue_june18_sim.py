import urllib.request, json

# Enqueue a SIM run for today 2026-06-18 with 12% daily loss limit
payload = {
    "source_date": "2026-06-18",
    "source_coll": "phase1_market_snapshots",
    "label": "june18_12pct_daily_loss",
    "speed": 0,
    "env_overrides": {
        "RISK_MAX_DAILY_LOSS_PCT": "0.12",
        "RISK_MAX_SESSION_TRADES": "20",
        "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED": "0",
        "ENTRY_ML_MIN_PROB": "0.049",
    }
}

data = json.dumps(payload).encode()
req = urllib.request.Request(
    "http://localhost:8008/api/sim/runs",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req, timeout=30) as resp:
    result = json.loads(resp.read())
    print("SIM enqueued:", json.dumps(result, indent=2))
    print("RUN_ID:", result.get("run_id"))

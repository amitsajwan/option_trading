import requests, json

API = "http://localhost:8008"

payload = {
    "source_date": "2026-06-02",
    "source_coll": "phase1_market_snapshots",
    "label": "june-tuned-cap20",
    "speed": 30.0,
    "env_overrides": {
        "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED": "0",
        "ENTRY_ML_MODEL_PATH": "/app/.data/ml_pipeline/entry_only_bundles/velocity_base",
        "ENTRY_ML_MIN_PROB": "0.049",
        "RISK_MAX_SESSION_TRADES": "20"
    }
}

r = requests.post(f"{API}/api/sim/runs", json=payload, timeout=30)
print(r.status_code, r.json() if r.ok else r.text)

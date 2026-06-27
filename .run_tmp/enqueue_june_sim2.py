import requests, json

API = "http://localhost:8008"

payload = {
    "trade_date_ist": "2026-06-02",
    "run_type": "deterministic_historical",
    "ml_entry": {
        "enabled": True,
        "min_prob": 0.049
    },
    "entry_gates": {
        "time_window_check": False,
        "atr_gate": False,
        "volume_gate": False
    },
    "env_overrides": {
        "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED": "0",
        "ENTRY_ML_MODEL_PATH": "/app/.data/ml_pipeline/entry_only_bundles/velocity_base",
        "ENTRY_ML_MIN_PROB": "0.049"
    }
}

r = requests.post(f"{API}/api/sim/runs", json=payload, timeout=30)
print(r.status_code, r.json() if r.ok else r.text)

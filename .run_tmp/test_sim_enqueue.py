import urllib.request, json
import sys

def enqueue(label, env_overrides):
    payload = {
        "source_date": "2026-06-18",
        "source_coll": "phase1_market_snapshots",
        "label": label,
        "speed": 1,
        "env_overrides": env_overrides,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://localhost:8008/api/sim/runs",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        r = json.loads(resp.read())
    print("enqueued:", r)
    return r.get("run_id")


def poll(rid):
    import time
    for i in range(30):
        req = urllib.request.Request(f"http://localhost:8008/api/sim/runs/{rid}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        status = d.get("status")
        print(f"  {rid}: {status} {d.get('metadata',{}).get('collection_counts',{})}")
        if status in ("completed", "failed", "error"):
            return d
        time.sleep(2)
    return None


if __name__ == "__main__":
    rid = enqueue("quick_test", {"RISK_MAX_DAILY_LOSS_PCT": "0.12"})
    print("polling...")
    poll(rid)

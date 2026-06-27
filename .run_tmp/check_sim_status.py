import requests, sys, time

API = "http://localhost:8008"
RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "9b71aeb0-9d41-4ce6-ab81-1827d2d8a2a1"

r = requests.get(f"{API}/api/sim/runs/{RUN_ID}", timeout=10)
if r.ok:
    d = r.json()
    print("status:", d.get("status"))
    print("progress:", d.get("progress", {}).get("percent", 0), "%")
    print("bars:", d.get("progress", {}).get("processed_bars"), "/", d.get("progress", {}).get("total_bars"))
    print("signals:", d.get("signals_count"))
    print("positions:", d.get("positions_count"))
    print("trades:", d.get("trade_count"))
    print("blocker:", d.get("terminal_blocker"))
else:
    print(r.status_code, r.text)

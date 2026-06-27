import urllib.request, json, sys

url = "http://localhost:8008/api/ops/config"
try:
    with urllib.request.urlopen(url, timeout=10) as resp:
        d = json.loads(resp.read())
    print("Total keys:", len(d))
    for k, v in d.items():
        if any(s in k for s in ["ML", "ENTRY", "RISK", "EXECUTION", "SIDEWAYS"]):
            print(f"  {k} = {v}")
except Exception as e:
    print("ERROR:", e)
    sys.exit(1)

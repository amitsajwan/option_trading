import json, sys

for line in sys.stdin:
    try:
        d = json.loads(line)
        if d.get("snapshot_id") == "20260601_1002":
            print(json.dumps(d, indent=2, default=str))
            break
    except Exception:
        pass

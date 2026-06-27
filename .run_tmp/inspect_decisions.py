import json, sys

for line in sys.stdin:
    try:
        d = json.loads(line)
        if d.get("blocker") == "no_selection":
            print(d.get("snapshot_id"), d.get("vote_count"), d.get("entry_vote_count"))
    except Exception:
        pass

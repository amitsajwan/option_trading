import json, sys

for line in sys.stdin:
    try:
        d = json.loads(line)
        if d.get("blocking_gate") == "no_selection":
            inp = d.get("input", {})
            print(d.get("snapshot_id"), d.get("blocking_gate"), inp.get("vote_count"), inp.get("entry_vote_count"))
    except Exception:
        pass

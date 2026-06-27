import json, sys

for line in sys.stdin:
    try:
        d = json.loads(line)
        if d.get("snapshot_id") == "20260601_1002":
            raw = d.get("raw_signals", {})
            print("policy_allowed:", raw.get("_policy_allowed", "NOT_SET"))
            print("policy_reason:", raw.get("_policy_reason", "NOT_SET"))
            print("entry_policy_mode:", raw.get("_entry_policy_mode", "NOT_SET"))
            print("proposed_strike:", d.get("proposed_strike"))
            print("proposed_entry_premium:", d.get("proposed_entry_premium"))
            break
    except Exception:
        pass

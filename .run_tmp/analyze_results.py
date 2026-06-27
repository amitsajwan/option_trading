import json, sys
from collections import Counter

decisions_file = "/opt/option_trading/.run/strategy_app_sim/ac436e95-22b7-4521-b975-9b974f14a9d2/decisions.jsonl"

blockers = Counter()
signals = 0
for line in open(decisions_file):
    d = json.loads(line)
    if d.get("action") == "blocked":
        blockers[d.get("blocking_gate", "unknown")] += 1
    elif d.get("action") == "signal":
        signals += 1

print(f"Signals: {signals}")
print(f"Blocked: {sum(blockers.values())}")
print("\nBlocker distribution:")
for b, c in blockers.most_common():
    print(f"  {b}: {c}")

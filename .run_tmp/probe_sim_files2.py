import json
from pathlib import Path
from collections import Counter

RUN_DIR = Path("/opt/option_trading/.run/strategy_app_sim/32fca5d4-d648-4848-a74d-9b9f0c44a792")

# decisions.jsonl - what fields does it have?
print("=== decisions.jsonl structure ===")
lines = [l.strip() for l in (RUN_DIR / "decisions.jsonl").open() if l.strip()]
d = json.loads(lines[0])
print("keys:", list(d.keys()))
print("sample:", {k: d[k] for k in list(d.keys())[:12]})

print("\n=== decisions.jsonl 'action' breakdown ===")
blocker_counts = Counter()
signal_count = 0
for l in lines:
    row = json.loads(l)
    outcome = row.get("outcome") or row.get("action") or "?"
    if outcome in ("signal", "entry", "trade"):
        signal_count += 1
    blocker = row.get("blocking_gate") or row.get("blocker") or row.get("block_reason") or ""
    if blocker:
        blocker_counts[blocker] += 1

print(f"  signal count: {signal_count}")
print(f"  blocked count: {sum(blocker_counts.values())}")
print(f"  top blockers: {blocker_counts.most_common(10)}")

# Find all unique keys across decisions
all_keys = set()
for l in lines[:50]:
    all_keys.update(json.loads(l).keys())
print("\nAll keys in decisions.jsonl:", sorted(all_keys))

print("\n=== decision_traces.jsonl sample ===")
dt_lines = [l.strip() for l in (RUN_DIR / "decision_traces.jsonl").open() if l.strip()]
if dt_lines:
    d = json.loads(dt_lines[0])
    print("keys:", list(d.keys()))
    print("blocking_gate:", d.get("blocking_gate"))
    print("outcome:", d.get("outcome"))
    print("sample:", {k: v for k, v in list(d.items())[:8]})
    # breakdown
    outcomes = Counter()
    blockers = Counter()
    for l in dt_lines:
        row = json.loads(l)
        outcomes[row.get("outcome", "?")] += 1
        bg = row.get("blocking_gate") or row.get("blocker") or ""
        if bg:
            blockers[bg] += 1
    print("outcomes:", outcomes)
    print("top blockers:", blockers.most_common(10))

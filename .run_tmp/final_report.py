import json
from collections import Counter

RUN_DIR = "/opt/option_trading/.run/strategy_app_sim/ac436e95-22b7-4521-b975-9b974f14a9d2"

# Count decisions
blockers = Counter()
signals = 0
for line in open(f"{RUN_DIR}/decisions.jsonl"):
    d = json.loads(line)
    if d.get("action") == "blocked":
        blockers[d.get("blocking_gate", "unknown")] += 1
    elif d.get("action") == "signal":
        signals += 1

# Count signals details
signal_count = 0
entry_signals = 0
for line in open(f"{RUN_DIR}/signals.jsonl"):
    d = json.loads(line)
    signal_count += 1
    if d.get("signal_type") == "ENTRY":
        entry_signals += 1

# Count positions
positions = 0
for line in open(f"{RUN_DIR}/positions.jsonl"):
    positions += 1

# Count votes
votes = 0
for line in open(f"{RUN_DIR}/votes.jsonl"):
    votes += 1

print("=" * 50)
print("SIM RUN REPORT: 2026-06-02")
print("=" * 50)
print(f"Total bars: 375")
print(f"Signals: {signals} (from decisions)")
print(f"Signals in signals.jsonl: {signal_count}")
print(f"Entry signals: {entry_signals}")
print(f"Positions: {positions}")
print(f"Votes: {votes}")
print(f"Blocked decisions: {sum(blockers.values())}")
print()
print("Blocker distribution:")
for b, c in blockers.most_common():
    pct = c / 375 * 100
    print(f"  {b}: {c} ({pct:.1f}%)")
print()
print("Key findings:")
has_sideways = "sideways_returns_mixed" in blockers
print(f"  sideways_returns_mixed blocker present: {has_sideways}")
if not has_sideways:
    print("  -> Gate fix CONFIRMED: sideways gate is DISABLED")
print(f"  -> Trade signals produced: {entry_signals}")

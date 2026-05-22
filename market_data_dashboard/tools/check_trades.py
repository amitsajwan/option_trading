#!/usr/bin/env python3
"""Check trades distribution for a run."""
import json
import sys

# Read from stdin
d = json.load(sys.stdin)
rows = d.get('rows', [])
print(f"Total days: {len(rows)}")
print()

for r in rows:
    date = r.get('date', 'N/A')
    trades = r.get('trades', 0)
    wins = r.get('wins', 0)
    losses = r.get('losses', 0)
    win_rate = r.get('win_rate', 0)
    print(f"{date}: {trades} trades ({wins} wins, {losses} losses, {win_rate:.1%} win rate)")

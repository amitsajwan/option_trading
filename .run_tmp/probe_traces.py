import json
from pathlib import Path

p = Path('c:/code/option_trading/option_trading_repo/.run_tmp/traces_a.jsonl')
lines = p.read_text().splitlines()
print('total traces:', len(lines))

# Print all keys from first trace
first = json.loads(lines[0])
print('keys in first trace:', first.keys())
print(json.dumps(first, indent=2)[:2000])

# Find traces with some signal or decision info
for i, line in enumerate(lines):
    d = json.loads(line)
    if any(d.get(k) for k in ['signal', 'candidates', 'votes', 'entry_signal', 'blocked_reason']):
        print(f'\n--- trace {i} at {d.get("timestamp")} ---')
        print(json.dumps(d, indent=2)[:2000])
        break
else:
    print('no signal/candidate/vote traces found')

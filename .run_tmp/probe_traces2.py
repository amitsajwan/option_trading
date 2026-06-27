import json
from pathlib import Path

p = Path('c:/code/option_trading/option_trading_repo/.run_tmp/traces_a.jsonl')
lines = p.read_text().splitlines()
first = json.loads(lines[0])
print('keys:', list(first.keys()))
for k, v in first.items():
    if isinstance(v, dict):
        print(f'{k}: dict with keys {list(v.keys())}')
    elif isinstance(v, list):
        print(f'{k}: list len {len(v)}')
        if v:
            print('  first item keys:', list(v[0].keys()) if isinstance(v[0], dict) else type(v[0]))
    else:
        print(f'{k}: {v}')

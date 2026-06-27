import json
from pathlib import Path

p = Path('c:/code/option_trading/option_trading_repo/.run_tmp/traces_a.jsonl')
lines = p.read_text().splitlines()
print('total traces:', len(lines))

# Find entries with candidate/signal
entries = []
for line in lines:
    d = json.loads(line)
    if d.get('candidates') or d.get('signal') or d.get('action') != 'PASS':
        entries.append(d)

print('non-pass traces:', len(entries))

# Look at first entry with features
for d in entries[:5]:
    print('---')
    print('time:', d.get('timestamp'), 'action:', d.get('action'))
    print('blocker:', d.get('blocker'))
    print('regime:', d.get('regime'))
    feats = d.get('features', {})
    print('features count:', len(feats))
    nan = sum(1 for v in feats.values() if isinstance(v, float) and v != v)
    print('NaN:', nan)
    if feats:
        print('sample keys:', list(feats.keys())[:10])
    # raw signals
    sig = d.get('signal', {})
    print('signal:', sig)

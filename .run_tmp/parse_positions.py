import json
from pathlib import Path

for label, p in [
    ('A', Path('c:/code/option_trading/option_trading_repo/.run_tmp/positions_a.jsonl')),
    ('B', Path('c:/code/option_trading/option_trading_repo/.run_tmp/positions_b.jsonl')),
]:
    print(f"\n=== {label} ===")
    closes = []
    opens = []
    for line in p.read_text().splitlines():
        d = json.loads(line)
        if d.get('event') == 'POSITION_OPEN':
            opens.append(d)
        elif d.get('event') == 'POSITION_CLOSE':
            closes.append(d)
    for o in opens:
        print(f"OPEN {o['position_id']} {o['direction']} {o['strike']} @ {o['entry_premium']} ({o['entry_time']}) conf={o.get('decision_metrics',{}).get('confidence')} reason={o['entry_reason']}")
    for c in closes:
        print(f"CLOSE {c['position_id']} {c.get('exit_reason')} @ {c.get('exit_premium')} pnl={c.get('pnl_pct'):.2%} mfe={c.get('mfe_pct'):.2%} mae={c.get('mae_pct'):.2%} bars={c.get('bars_held')} ({c.get('exit_time')})")
    # net
    net = sum(c.get('pnl_pct',0) for c in closes)
    print(f"NET PNL from closes: {net:.2%}")

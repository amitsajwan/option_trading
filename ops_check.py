import json, sys
from pathlib import Path

base = Path('/opt/option_trading/.run/strategy_app')

# ---- Closed positions ----
positions = {}
for line in (base / 'positions.jsonl').read_text().splitlines():
    try:
        d = json.loads(line)
        pid = d.get('position_id','')
        evt = d.get('event','')
        if evt in ('POSITION_OPEN','POSITION_MANAGE','POSITION_CLOSE'):
            if evt == 'POSITION_CLOSE' or pid not in positions:
                positions[pid] = d
    except:
        pass

closed = [p for p in positions.values() if p.get('event') == 'POSITION_CLOSE']
closed.sort(key=lambda x: str(x.get('timestamp','')))

print('\n=== LAST 10 CLOSED TRADES ===')
print('  %-8s  %5s  %2s  %10s  %7s  %7s  %4s  %s' % ('pid','time','dr','entry_prem','pnl%','mfe%','bars','exit'))
print('-'*80)
for p in closed[-10:]:
    ts = str(p.get('timestamp',''))
    hhmm = ts[11:16] if len(ts) > 15 else '?'
    ep = float(p.get('entry_premium') or 0)
    exit_pol = p.get('exit_policy_triggered') or ''
    exit_r = p.get('exit_reason','')
    label = exit_pol if exit_pol else exit_r
    pnl = float(p.get('pnl_pct') or 0) * 100
    mfe = float(p.get('mfe_pct') or 0) * 100
    bars = int(p.get('bars_held') or 0)
    dr = p.get('direction','')
    print('  %-8s  %5s  %2s  %10.1f  %6.2f%%  %6.2f%%  %4d  %s' % (
        p['position_id'][:8], hhmm, dr, ep, pnl, mfe, bars, label))

# ---- Latest decision trace for ML context ----
traces_path = base / 'decision_traces.jsonl'
lines = traces_path.read_text().splitlines()
# get last 5 traces
print('\n=== LAST 5 DECISION TRACES ===')
for line in lines[-5:]:
    try:
        d = json.loads(line)
        sid = d.get('snapshot_id','')
        outcome = d.get('final_outcome','')
        regime = (d.get('regime_context') or {}).get('regime','')
        epath = d.get('execution_path','')
        cands = d.get('candidates') or []
        conf = ''
        dir_info = ''
        entry_prem = ''
        for c in cands:
            if c.get('terminal_status') == 'passed' or c.get('selected'):
                conf = '%.2f' % float(c.get('confidence') or 0)
                dir_info = str(c.get('direction',''))
                for g in (c.get('ordered_gates') or []):
                    if g.get('gate_id') == 'execution':
                        m = g.get('metrics') or {}
                        entry_prem = str(m.get('entry_premium',''))
                break
        print('  %s  %s  regime=%-15s  conf=%s  dir=%s  prem=%s  path=%s' % (
            sid, outcome[:20].ljust(20), regime, conf, dir_info, entry_prem, epath))
    except Exception as e:
        print('  parse error:', e)

# ---- Check ML usage from a fired trade trace ----
print('\n=== ML USAGE ON LAST ENTRY_TAKEN TRACE ===')
entry_traces = []
for line in lines:
    try:
        d = json.loads(line)
        if d.get('final_outcome') == 'entry_taken':
            entry_traces.append(d)
    except:
        pass

if entry_traces:
    last = entry_traces[-1]
    print('  snapshot:', last.get('snapshot_id'))
    print('  path:', last.get('execution_path'))
    cands = last.get('candidates') or []
    for c in cands:
        if c.get('terminal_status') == 'passed' or c.get('selected'):
            print('  strategy:', c.get('strategy_name'))
            print('  confidence:', c.get('confidence'))
            print('  direction:', c.get('direction'))
            for g in (c.get('ordered_gates') or []):
                gid = g.get('gate_id','')
                m = g.get('metrics') or {}
                if gid in ('confidence_gate','direction_consensus','execution','policy_checks'):
                    print('    gate %-25s  %s  %s' % (gid, g.get('status',''), m))
else:
    print('  no entry_taken traces found today')

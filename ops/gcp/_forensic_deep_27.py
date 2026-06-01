import json, re
from collections import defaultdict
from pathlib import Path

rid='a1d74e6c-8121-49e6-bc54-64b9c0b920f0'
root=Path('/opt/option_trading/.run/strategy_app_sim')/rid
votes=[json.loads(x) for x in (root/'votes.jsonl').read_text().splitlines() if x.strip()]
pos=[json.loads(x) for x in (root/'positions.jsonl').read_text().splitlines() if x.strip()]
opens=[r for r in pos if r.get('event')=='POSITION_OPEN']
closes=[r for r in pos if r.get('event')=='POSITION_CLOSE']
close_by_id={r['position_id']:r for r in closes}
ml_votes=[r for r in votes if r.get('strategy')=='ML_ENTRY' and r.get('signal_type')=='ENTRY']
by_snap={r.get('snapshot_id'):r for r in ml_votes}

trades=[]
for o in opens:
    c=close_by_id.get(o['position_id'])
    if not c:
        continue
    v=by_snap.get(o.get('snapshot_id'))
    rs=(v or {}).get('raw_signals') or {}
    src=str(rs.get('direction_source') or 'unknown')
    entry_sources=rs.get('entry_dir_sources') or {}
    pnl=float(c.get('pnl_pct') or 0.0)
    trades.append({
        'position_id':o['position_id'],
        'direction':o.get('direction'),
        'snapshot_id':o.get('snapshot_id'),
        'pnl_pct':pnl,
        'direction_source':src,
        'entry_dir_sources':entry_sources,
        'strike':o.get('strike'),
        'entry_premium':o.get('entry_premium'),
        'exit_reason':c.get('reason'),
    })

print('total_trades',len(trades))
wins=sum(1 for t in trades if t['pnl_pct']>0)
print('overall_win_rate_pct', round(100.0*wins/len(trades),1) if trades else None)
print('overall_net_pct', round(sum(t['pnl_pct'] for t in trades),4) if trades else None)

for side in ('CE','PE'):
    grp=[t for t in trades if t['direction']==side]
    if not grp:
        continue
    w=sum(1 for t in grp if t['pnl_pct']>0)
    print('side',side,'n',len(grp),'win_rate_pct',round(100.0*w/len(grp),1),'net_pct',round(sum(t['pnl_pct'] for t in grp),4),'avg_pct',round(sum(t['pnl_pct'] for t in grp)/len(grp),4))

print('\nby_direction_source:')
agg=defaultdict(list)
for t in trades:
    agg[t['direction_source']].append(t['pnl_pct'])
for src, vals in sorted(agg.items(), key=lambda kv: len(kv[1]), reverse=True):
    w=sum(1 for x in vals if x>0)
    print(src,':: n',len(vals),'wr',round(100.0*w/len(vals),1),'net',round(sum(vals),4),'avg',round(sum(vals)/len(vals),4))

print('\nby_signal_token (from entry_dir_sources, min_n=2):')
toks=defaultdict(list)
for t in trades:
    for k in (t['entry_dir_sources'] or {}).keys():
        toks[str(k)].append(t['pnl_pct'])
for k, vals in sorted(toks.items(), key=lambda kv:(len(kv[1]), sum(kv[1])), reverse=True):
    if len(vals) < 2:
        continue
    w=sum(1 for x in vals if x>0)
    print(k,':: n',len(vals),'wr',round(100.0*w/len(vals),1),'net',round(sum(vals),4),'avg',round(sum(vals)/len(vals),4))

print('\ntrades_detail:')
for t in trades:
    print(json.dumps(t, ensure_ascii=False))

import json, time, urllib.request
from pathlib import Path
run_id='a1d74e6c-8121-49e6-bc54-64b9c0b920f0'
url=f'http://127.0.0.1:8008/api/sim/runs/{run_id}'
for i in range(20):
    with urllib.request.urlopen(url, timeout=15) as r:
        row=json.loads(r.read().decode())
    st=row.get('status')
    print(f'check {i+1} status={st}')
    if st in {'completed','failed','cancelled'}:
        break
    time.sleep(60)

root=Path('/opt/option_trading/.run/strategy_app_sim')/run_id
rc=root/'runtime_config.json'
if rc.exists():
    cfg=json.loads(rc.read_text())
    print('runtime.strategy_profile_id',cfg.get('strategy_profile_id'))

for fn in ['votes.jsonl','signals.jsonl','positions.jsonl','decision_trace.jsonl']:
    p=root/fn
    print(fn,'exists',p.exists())

# positions forensic
p=root/'positions.jsonl'
if p.exists():
    rows=[json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    closes=[r for r in rows if str(r.get('event'))=='POSITION_CLOSE']
    pnl=[float(r.get('pnl_pct')) for r in closes if r.get('pnl_pct') is not None]
    sides=sorted({str(r.get('position_side')) for r in closes})
    wins=sum(1 for x in pnl if x>0)
    gp=sum(x for x in pnl if x>0)
    gl=-sum(x for x in pnl if x<0)
    pf=(gp/gl) if gl>0 else None
    print('closes',len(closes),'sides',sides,'net_pct',round(sum(pnl),4) if pnl else None,'win_rate',round(100*wins/len(pnl),1) if pnl else None,'pf',round(pf,3) if pf is not None else None)

# vote sources forensic
vp=root/'votes.jsonl'
if vp.exists():
    vrows=[json.loads(x) for x in vp.read_text().splitlines() if x.strip()]
    src={}
    strat={}
    for r in vrows:
      s=((r.get('raw_signals') or {}).get('direction_source') or 'none')
      src[s]=src.get(s,0)+1
      st=str(r.get('strategy') or '')
      strat[st]=strat.get(st,0)+1
    print('vote_strategies',sorted(strat.items(), key=lambda kv: kv[1], reverse=True)[:10])
    print('direction_sources',sorted(src.items(), key=lambda kv: kv[1], reverse=True)[:10])

# decision-trace diagnostics sample
tr=root/'decision_trace.jsonl'
if tr.exists():
    rows=[json.loads(x) for x in tr.read_text().splitlines() if x.strip()]
    diag=[]
    for r in rows:
        d=((r.get('raw_signals') or {}).get('entry_dir_sources'))
        if d:
            diag.append(d)
    print('trace_with_entry_dir_sources',len(diag),'of',len(rows))

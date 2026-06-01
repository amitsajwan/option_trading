import json, time, urllib.request
from collections import Counter
from pathlib import Path
rid='3ad248f5-73e9-4a46-80c7-ee84fa9edc80'
url=f'http://127.0.0.1:8008/api/sim/runs/{rid}'
for i in range(20):
  with urllib.request.urlopen(url,timeout=15) as r:
    row=json.loads(r.read().decode())
  st=row.get('status')
  print('check',i+1,'status',st)
  if st in {'completed','failed','cancelled'}:
    break
  time.sleep(60)

root=Path('/opt/option_trading/.run/strategy_app_sim')/rid
pos=[json.loads(x) for x in (root/'positions.jsonl').read_text().splitlines() if x.strip()]
opens=[r for r in pos if r.get('event')=='POSITION_OPEN']
closes=[r for r in pos if r.get('event')=='POSITION_CLOSE']
close_by={r['position_id']:r for r in closes}
tr=[]
for o in opens:
 c=close_by.get(o['position_id'])
 if not c: continue
 tr.append((o.get('strike'),float(c.get('pnl_pct') or 0.0),o.get('direction')))

print('trades',len(tr))
if tr:
 wins=sum(1 for _,p,_ in tr if p>0)
 net=sum(p for _,p,_ in tr)
 print('win_rate',round(100*wins/len(tr),1),'net_pct',round(net,4))
 print('strikes',Counter(s for s,_,_ in tr))
 print('dirs',Counter(d for _,_,d in tr))
 print('avg_pnl_by_strike', {k: round(sum(p for s,p,_ in tr if s==k)/len([1 for s,_,_ in tr if s==k]),4) for k in sorted({s for s,_,_ in tr})})

votes=[json.loads(x) for x in (root/'votes.jsonl').read_text().splitlines() if x.strip()]
ml=[v for v in votes if v.get('strategy')=='ML_ENTRY' and v.get('signal_type')=='ENTRY']
sp=Counter(((v.get('raw_signals') or {}).get('_strike_policy','none')) for v in ml)
sel=Counter(((v.get('raw_signals') or {}).get('_strike_selected','none')) for v in ml)
print('strike_policy_tags',sp)
print('strike_selected_top',sel.most_common(10))

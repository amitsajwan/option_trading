#!/usr/bin/env python3
import json
from collections import defaultdict
from pathlib import Path
from pymongo import MongoClient

RUN_ID = 'a1d74e6c-8121-49e6-bc54-64b9c0b920f0'
TRADE_DATE = '2026-05-27'
HORIZONS = [1, 3, 5, 10]

root = Path('/app/.run/strategy_app_sim') / RUN_ID
votes = [json.loads(x) for x in (root / 'votes.jsonl').read_text().splitlines() if x.strip()]
ml_votes = [v for v in votes if v.get('strategy') == 'ML_ENTRY' and v.get('signal_type') == 'ENTRY']

mc = MongoClient('mongo', 27017)
coll = mc['trading_ai']['phase1_market_snapshots']
raw_rows = list(coll.find({'trade_date_ist': TRADE_DATE}, {'_id': 0, 'snapshot_id': 1, 'payload.snapshot': 1}))
rows = []
for r in raw_rows:
    s = ((r.get('payload') or {}).get('snapshot') or {})
    sid = str(r.get('snapshot_id') or s.get('snapshot_id') or '')
    if not sid:
        continue
    rows.append({'snapshot_id': sid, 'snapshot': s})
rows.sort(key=lambda d: d['snapshot_id'])
idx_by_sid = {r['snapshot_id']: i for i, r in enumerate(rows)}

def strike_row(snap, strike):
    arr = snap.get('strikes') if isinstance(snap.get('strikes'), list) else []
    for r in arr:
        if not isinstance(r, dict):
            continue
        try:
            if int(float(r.get('strike'))) == int(strike):
                return r
        except Exception:
            continue
    return None

def opt_ltp(snap, direction, strike):
    row = strike_row(snap, strike)
    if not row:
        return None
    key = 'ce_ltp' if direction == 'CE' else 'pe_ltp'
    v = row.get(key)
    try:
        x = float(v)
        return x if x > 0 else None
    except Exception:
        return None

def pct(a, b):
    return (b - a) / a if a and a > 0 and b is not None else None

evals = []
for v in ml_votes:
    sid = str(v.get('snapshot_id') or '')
    if sid not in idx_by_sid:
        continue
    i = idx_by_sid[sid]
    direction = str(v.get('direction') or '')
    try:
        strike = int(v.get('proposed_strike'))
    except Exception:
        continue
    try:
        entry = float(v.get('proposed_entry_premium'))
    except Exception:
        entry = opt_ltp(rows[i]['snapshot'], direction, strike)
    if not entry or entry <= 0:
        continue

    rec = {
        'snapshot_id': sid,
        'direction': direction,
        'strike': strike,
        'entry_premium': entry,
        'confidence': float(v.get('confidence') or 0.0),
        'entry_prob': float((v.get('raw_signals') or {}).get('entry_prob') or 0.0),
        'direction_source': str(((v.get('raw_signals') or {}).get('direction_source') or 'none')),
    }
    wins=[]
    for h in HORIZONS:
        j=i+h
        val = opt_ltp(rows[j]['snapshot'], direction, strike) if j < len(rows) else None
        p=pct(entry,val)
        rec[f'pnl_h{h}']=p
        if p is not None:
            wins.append(p)
    rec['mfe_10']=max(wins) if wins else None
    rec['mae_10']=min(wins) if wins else None
    evals.append(rec)

print('ml_votes_total',len(ml_votes),'counterfactual_evaluable',len(evals))

def stat(vals):
    vals=[x for x in vals if x is not None]
    if not vals:
        return {'n':0,'wr':None,'avg':None}
    wr=100.0*sum(1 for x in vals if x>0)/len(vals)
    return {'n':len(vals),'wr':round(wr,1),'avg':round(sum(vals)/len(vals),4)}

for h in HORIZONS:
    print(f'ALL h{h}:', stat([r[f'pnl_h{h}'] for r in evals]))

buckets=[(0.65,0.75),(0.75,0.85),(0.85,0.95),(0.95,1.01)]
for lo,hi in buckets:
    grp=[r for r in evals if lo <= r['entry_prob'] < hi]
    print(f'entry_prob[{lo},{hi}) h5:', stat([r['pnl_h5'] for r in grp]))

src=defaultdict(list)
for r in evals:
    src[r['direction_source']].append(r['pnl_h5'])
print('TOP direction_source on h5:')
for k,v in sorted(src.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]:
    print(k, stat(v))

for d in ('CE','PE'):
    grp=[r for r in evals if r['direction']==d]
    print(f'side {d} h5', stat([r['pnl_h5'] for r in grp]))

Path('/tmp/counterfactual_ml_entry_all_votes_horizons.json').write_text(json.dumps(evals, indent=2), encoding='utf-8')
print('wrote /tmp/counterfactual_ml_entry_all_votes_horizons.json')

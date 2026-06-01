from pymongo import MongoClient
c=MongoClient('mongo',27017)['trading_ai']['phase1_market_snapshots']
d=c.find_one({'trade_date_ist':'2026-05-27'},{'_id':0,'payload.snapshot':1,'snapshot_id':1})
s=(d.get('payload') or {}).get('snapshot') or {}
print('snapshot_id_top', d.get('snapshot_id'))
print('snapshot_keys', sorted(s.keys())[:200])
print('has_strikes', isinstance(s.get('strikes'), list), 'len', len(s.get('strikes') or []))
if s.get('strikes'):
 print('strike_keys', sorted((s.get('strikes')[0] or {}).keys()))
 print('sample_strike', s.get('strikes')[0])

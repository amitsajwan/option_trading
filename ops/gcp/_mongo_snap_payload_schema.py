from pymongo import MongoClient
c=MongoClient('mongo',27017)['trading_ai']['phase1_market_snapshots']
d=c.find_one({'trade_date_ist':'2026-05-27'},{'_id':0})
print('found', bool(d))
if d:
 p=d.get('payload') or {}
 print('top_keys', sorted(d.keys()))
 print('payload_keys', sorted(p.keys())[:120])
 print('payload_snapshot_id', p.get('snapshot_id'))
 print('payload_trade_date', p.get('trade_date'))
 st=p.get('strikes') or []
 print('strikes_len', len(st))
 if st:
  print('strike_row_keys', sorted((st[0] or {}).keys()))

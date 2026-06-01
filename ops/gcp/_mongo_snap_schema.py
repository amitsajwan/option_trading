from pymongo import MongoClient
c=MongoClient('mongo',27017)['trading_ai']['phase1_market_snapshots']
d=c.find_one({}, {'_id':0})
print('keys', sorted(d.keys())[:80])
print('snapshot_id', d.get('snapshot_id'))
print('trade_date', d.get('trade_date'))
print('session_context', d.get('session_context'))
print('strikes_type', type(d.get('strikes')).__name__, 'len', len(d.get('strikes') or []))
if d.get('strikes'):
 r=d['strikes'][0]
 print('strike_row_keys', sorted(r.keys()))

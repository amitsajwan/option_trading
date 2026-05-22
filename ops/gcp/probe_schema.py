from pymongo import MongoClient
db = MongoClient('mongodb://mongo:27017').trading_ai
RUN = '9e3789a3-deb5-4dcd-ba8a-9a646a1033bd'
doc = db.strategy_positions_historical.find_one({'run_id': RUN, 'event': 'POSITION_CLOSE'})
if doc:
    print('TOP KEYS:', list(doc.keys()))
    for k, v in doc.items():
        if k != '_id':
            print(f'  {k}: {repr(v)[:200]}')
else:
    print('No POSITION_CLOSE found')
    # check what events exist
    for ev in db.strategy_positions_historical.distinct('event', {'run_id': RUN}):
        n = db.strategy_positions_historical.count_documents({'run_id': RUN, 'event': ev})
        print(f'  event={ev}  n={n}')

const db = connect('mongodb://localhost:27017/trading_ai');
const docs = db.phase1_market_snapshots.find({trade_date: '2026-06-18'}, {'snapshot.market_time_ist': 1}).sort({'snapshot.market_time_ist': 1}).limit(10).toArray();
printjson(docs.map(d => ({id: d._id, time: d.snapshot?.market_time_ist})));

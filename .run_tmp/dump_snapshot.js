const db = connect('mongodb://localhost:27017/trading_ai');
const snap = db.phase1_market_snapshots.findOne({ trade_date_ist: '2026-06-18', market_time_ist: '09:48:00' });
const fs = require('fs');
fs.writeFileSync('/tmp/snap_0948.json', JSON.stringify(snap));
print('written');

// Check features in one live snapshot for 2026-06-18
const db = connect('mongodb://localhost:27017/trading_ai');
const snap = db.phase1_market_snapshots.findOne({ trade_date: '2026-06-18', 'snapshot.market_time_ist': '10:40:00' });
if (!snap) {
    print('no snapshot');
    quit();
}
print('snapshot_id:', snap.snapshot_id);
print('keys in snapshot:', Object.keys(snap.snapshot).join(','));
const ml = snap.snapshot.ml_features || {};
print('ml_features keys:', Object.keys(ml).length);
// Check specific features
const toCheck = ['vix_intraday_chg', 'ctx_regime_1', 'ctx_regime_2', 'vel_oi_5', 'vel_oi_10', 'vix_current', 'vix_prev_close'];
for (const k of toCheck) {
    const v = ml[k];
    print(k + ':', typeof v === 'number' ? v : JSON.stringify(v));
}
// count NaNs
let nan = 0, total = 0;
for (const [k, v] of Object.entries(ml)) {
    if (typeof v === 'number') {
        total++;
        if (Number.isNaN(v)) nan++;
    }
}
print('NaN count:', nan, 'of', total, 'numeric features');

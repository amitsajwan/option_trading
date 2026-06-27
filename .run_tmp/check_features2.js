const db = connect('mongodb://localhost:27017/trading_ai');
const snap = db.phase1_market_snapshots.findOne({ trade_date_ist: '2026-06-18', market_time_ist: '10:40:00' });
if (!snap) {
    print('no snapshot');
    quit();
}
print('snapshot_id:', snap.snapshot_id);
const payload = snap.payload || {};
print('payload keys:', Object.keys(payload).join(','));
const ml = payload.ml_features || {};
print('ml_features keys:', Object.keys(ml).length);
const toCheck = ['vix_intraday_chg', 'ctx_regime_1', 'ctx_regime_2', 'vel_oi_5', 'vel_oi_10', 'vix_current', 'vix_prev_close'];
for (const k of toCheck) {
    const v = ml[k];
    print(k + ':', typeof v === 'number' ? v : JSON.stringify(v));
}
let nan = 0, total = 0;
for (const [k, v] of Object.entries(ml)) {
    if (typeof v === 'number') {
        total++;
        if (Number.isNaN(v)) nan++;
    }
}
print('NaN count:', nan, 'of', total, 'numeric features');

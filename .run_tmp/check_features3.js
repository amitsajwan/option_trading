const db = connect('mongodb://localhost:27017/trading_ai');
const snap = db.phase1_market_snapshots.findOne({ trade_date_ist: '2026-06-18', market_time_ist: '10:40:00' });
const payload = snap.payload || {};
print('payload keys:', Object.keys(payload).join(','));
const snapshot = payload.snapshot || {};
print('snapshot keys:', Object.keys(snapshot).join(','));
print('vix_context keys:', Object.keys(snapshot.vix_context || {}).join(','));
print('regime_context keys:', Object.keys(snapshot.regime_context || {}).join(','));
print('ml_features_v2 keys:', Object.keys(snapshot.ml_features_v2 || {}).join(','));
print('ml_features keys:', Object.keys(snapshot.ml_features || {}).join(','));
const ml = snapshot.ml_features_v2 || snapshot.ml_features || {};
print('ml count:', Object.keys(ml).length);
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

var signals = db.trade_signals.countDocuments({trade_date_ist: "2026-06-18", run_id: null});
var positions = db.strategy_positions.countDocuments({trade_date_ist: "2026-06-18", run_id: null});
print("live_signals=" + signals + " live_positions=" + positions);

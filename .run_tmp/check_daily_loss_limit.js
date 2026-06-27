// Check what risk env vars are active
var date = "2026-06-18";

// Check ops_env.json via strategy_app
var snap = db.phase1_market_snapshots.find({trade_date_ist: date}).sort({market_time_ist: -1}).limit(1).next();
print("Last snapshot time: " + snap.market_time_ist);
print("Total snapshots today: " + db.phase1_market_snapshots.countDocuments({trade_date_ist: date}));

// Check positions closed today with reason
var closed = db.strategy_positions.find({trade_date_ist: date, event: "POSITION_CLOSE"}).toArray();
print("\nClosed positions today: " + closed.length);
closed.forEach(function(p) {
    print("  " + p.direction + " " + p.strike + " pnl=" + (p.pnl_pct*100).toFixed(2) + "% mfe=" + (p.mfe_pct*100).toFixed(2) + "% mae=" + (p.mae_pct*100).toFixed(2) + "% exit=" + p.exit_reason + " bars=" + p.bars_held);
});

// Check execution_fills
var ef = db.execution_fills.find({trade_date_ist: date}).toArray();
print("\nExecution fills: " + ef.length);
ef.forEach(function(f) {
    print("  " + f.side + " " + (f.instrument||"?") + " status=" + f.status + " order_id=" + f.order_id + " err=" + (f.error||"none"));
});

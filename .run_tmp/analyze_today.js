// Today's session analysis for 2026-06-18
var date = "2026-06-18";

// 1. Snapshot count
var snapCount = db.phase1_market_snapshots.countDocuments({trade_date_ist: date});
print("=== SESSION OVERVIEW ===");
print("Snapshots today: " + snapCount);

// 2. Live signals (run_id null = live, not SIM)
var liveSigs = db.trade_signals.find({trade_date_ist: date}).toArray();
print("Live signals today: " + liveSigs.length);
liveSigs.forEach(function(s) {
    print("  SIGNAL " + s.signal_type + " " + s.direction + " " + s.strike + " @ " + s.entry_premium + " | conf=" + (s.confidence||"?") + " | time=" + s.timestamp.substring(11,16) + " | run_id=" + s.run_id);
});

// 3. Live positions
var livePos = db.strategy_positions.find({trade_date_ist: date}).toArray();
print("\nPositions today: " + livePos.length);
livePos.forEach(function(p) {
    print("  POS " + p.event + " " + p.direction + " " + p.strike + " pnl=" + (p.pnl_pct ? (p.pnl_pct*100).toFixed(2)+"%" : "open") + " exit=" + (p.exit_reason||"open") + " run_id=" + p.run_id);
});

// 4. Execution fills
var fills = db.execution_fills.find({trade_date_ist: date}).toArray();
print("\nExecution fills today: " + fills.length);
fills.forEach(function(f) {
    print("  FILL " + f.side + " " + f.instrument + " status=" + f.status + " order_id=" + f.order_id + " time=" + f.filled_at);
});

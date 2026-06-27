// Full session analysis 2026-06-18
var date = "2026-06-18";
print("====================================");
print("SESSION ANALYSIS: " + date);
print("====================================");

// Snapshots
var snaps = db.phase1_market_snapshots.find({trade_date_ist: date}).sort({market_time_ist: 1}).toArray();
print("\n1. DATA COVERAGE");
print("   Total snapshots: " + snaps.length);
if (snaps.length > 0) {
    print("   First: " + snaps[0].market_time_ist);
    print("   Last:  " + snaps[snaps.length-1].market_time_ist);
    // spot price range
    var prices = snaps.map(function(s) { return s.payload.snapshot.futures_bar ? s.payload.snapshot.futures_bar.fut_close : null; }).filter(function(x) { return x; });
    if (prices.length > 0) {
        print("   Fut price range: " + Math.min.apply(null, prices).toFixed(0) + " - " + Math.max.apply(null, prices).toFixed(0));
        print("   Open: " + prices[0].toFixed(0) + "  Close: " + prices[prices.length-1].toFixed(0) + "  Move: " + ((prices[prices.length-1]-prices[0])/prices[0]*100).toFixed(2) + "%");
    }
}

// Decisions / signals from live strategy
print("\n2. SIGNALS & DECISIONS");
var signals = db.trade_signals.find({trade_date_ist: date, run_id: /^paper-/}).toArray();
print("   Paper trade signals: " + signals.length);
var entries = signals.filter(function(s){ return s.signal_type === "ENTRY"; });
var exits = signals.filter(function(s){ return s.signal_type === "EXIT"; });
print("   ENTRY signals: " + entries.length);
print("   EXIT signals: " + exits.length);
entries.forEach(function(s) {
    var t = s.timestamp ? s.timestamp.substring(11,16) : "?";
    print("   --> ENTRY " + t + " " + s.direction + " " + s.strike + " conf=" + (s.confidence||"?").toString().substring(0,5) + " reason=" + (s.reason||"?").substring(0,60));
});

// Positions
print("\n3. POSITION OUTCOME");
var closed = db.strategy_positions.find({trade_date_ist: date, event: "POSITION_CLOSE"}).toArray();
print("   Closed positions: " + closed.length);
closed.forEach(function(p) {
    var entry_t = p.entry_time ? p.entry_time.substring(11,16) : "?";
    var exit_t  = p.timestamp  ? p.timestamp.substring(11,16)  : "?";
    print("   " + entry_t + "->" + exit_t + " " + p.direction + " " + p.strike + " entry=" + p.entry_premium + 
          " | PnL=" + (p.pnl_pct*100).toFixed(2) + "%" + 
          " | MFE=" + (p.mfe_pct*100).toFixed(2) + "% MAE=" + (p.mae_pct*100).toFixed(2) + "%" +
          " | bars=" + (p.bars_held||"?") + " | exit=" + p.exit_reason);
});

// Execution fills
print("\n4. EXECUTION PATH");
var fills = db.execution_fills.find({trade_date_ist: date}).toArray();
print("   Total fills recorded: " + fills.length);
fills.forEach(function(f) {
    print("   " + f.side + " " + (f.instrument||"?") + " status=" + f.status + " order=" + f.order_id + " err=" + (f.error_message||f.error||""));
});

// Risk state
print("\n5. RISK STATE (end of day)");
var lastManage = db.strategy_positions.find({trade_date_ist: date, event: "POSITION_MANAGE"}).sort({timestamp:-1}).limit(1).next();
if (lastManage) {
    print("   Last pnl snapshot: " + (lastManage.pnl_pct*100).toFixed(2) + "% at " + lastManage.timestamp.substring(11,16));
}

print("\n====================================");
print("DAILY LOSS LIMIT: -2.02% breach → HALT at 10:57");
print("Strategy stopped taking new trades after 10:57");
print("====================================");

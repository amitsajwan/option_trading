var date = "2026-06-18";
var closed = db.strategy_positions.find({trade_date_ist: date, event: "POSITION_CLOSE"}).toArray();
closed.forEach(function(p) {
    print(JSON.stringify({
        dir: p.direction,
        strike: p.strike,
        pnl: p.pnl_pct,
        mfe: p.mfe_pct,
        mae: p.mae_pct,
        bars: p.bars_held,
        exit: p.exit_reason,
        entry_time: p.entry_time,
        exit_time: p.timestamp,
        entry_premium: p.entry_premium
    }));
});

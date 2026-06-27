var count = db.phase1_market_snapshots.countDocuments({trade_date_ist: "2026-06-18"});
print("today_snapshots=" + count);

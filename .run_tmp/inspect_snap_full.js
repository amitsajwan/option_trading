var d = db.phase1_market_snapshots.find({trade_date_ist: "2026-06-18"}).sort({market_time_ist:-1}).limit(1).next();
print(JSON.stringify(d, null, 2).substring(0, 2000));

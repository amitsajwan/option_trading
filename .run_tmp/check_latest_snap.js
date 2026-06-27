var d = db.phase1_market_snapshots.find().sort({market_time_ist:-1}).limit(1).next();
print(d.trade_date_ist + " " + d.market_time_ist);

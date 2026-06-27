var d = db.phase1_market_snapshots.find({trade_date_ist: "2026-06-18"}).sort({market_time_ist:-1}).limit(1).next();
print("date=" + d.trade_date_ist + " time=" + d.market_time_ist);
print("instrument=" + d.payload.snapshot.instrument_symbol);
print("fut_price=" + d.payload.snapshot.futures_price);

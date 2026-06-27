var d = db.phase1_market_snapshots.find({trade_date_ist: "2026-06-18"}).sort({market_time_ist:-1}).limit(1).next();
print("date=" + d.trade_date_ist + " time=" + d.market_time_ist);
print("payload keys: " + Object.keys(d.payload).join(", "));
print("snapshot keys: " + Object.keys(d.payload.snapshot).join(", "));
print("instrument_symbol=" + (d.payload.snapshot.instrument_symbol || "NULL"));
print("futures_price=" + (d.payload.snapshot.futures_price || "NULL"));

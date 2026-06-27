var dates = db.phase1_market_snapshots.distinct("trade_date_ist").sort();
print("Total dates: " + dates.length);
print("Latest 15:");
dates.slice(-15).forEach(d => print(d));

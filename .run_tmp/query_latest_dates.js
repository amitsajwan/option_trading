var dates = db.snapshots.distinct("trade_date_ist").sort();
print("Total dates: " + dates.length);
print("Latest 10:");
dates.slice(-10).forEach(d => print(d));

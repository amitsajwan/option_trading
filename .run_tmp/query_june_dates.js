var dates = db.phase1_market_snapshots.distinct("trade_date_ist", {trade_date_ist: {$gte: "2026-06-01", $lte: "2026-06-30"}});
dates.sort();
print("JUNE_2026_DATES=" + dates.join(","));
for (var i = 0; i < dates.length; i++) {
    var d = dates[i];
    var cnt = db.phase1_market_snapshots.countDocuments({trade_date_ist: d});
    print("  " + d + ": " + cnt + " snapshots");
}

db.snapshots.distinct("trade_date_ist", {trade_date_ist: {$gte: "2026-07-01", $lte: "2026-07-31"}}).sort().forEach(d => print(d))

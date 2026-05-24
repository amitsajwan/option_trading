const runs = [
  { id: "e8ba040a-a8dd-47d1-9bf8-ceffba85e809", label: "det_dir" },
  { id: "4c345e18-edac-41b3-968d-06d3290b8549", label: "ml_entry_v1" },
];
for (const r of runs) {
  const n = db.strategy_votes_historical.countDocuments({
    run_id: r.id,
    strategy: "ML_ENTRY",
    trade_date_ist: { $gte: "2024-08-26", $lte: "2024-10-07" },
  });
  const trades = db.strategy_positions_historical.countDocuments({
    run_id: r.id,
    event: "POSITION_CLOSE",
    trade_date_ist: { $gte: "2024-08-26", $lte: "2024-10-07" },
  });
  print(r.label, "ml_votes_overlap", n, "trades_overlap", trades);
}

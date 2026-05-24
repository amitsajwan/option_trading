db = db.getSiblingDB("trading_ai");
var d = db.strategy_positions_historical.findOne({trade_date_ist: {$gte: "2024-08-01", $lte: "2024-10-31"}});
if (d) {
  print("KEYS: " + Object.keys(d).join(", "));
  print("event_type: " + d.event_type);
  print("event: " + d.event);
  print("exit_reason: " + d.exit_reason);
  print("pnl_pct: " + d.pnl_pct);
  print("direction: " + d.direction);
  print("trade_date_ist: " + d.trade_date_ist);
  print("reason: " + String(d.reason||"").slice(0,120));
} else {
  print("NO DOCS in 2024-08-01 to 2024-10-31");
  var any = db.strategy_positions_historical.findOne();
  if (any) {
    print("Sample trade_date_ist: " + any.trade_date_ist);
    print("Sample event_type: " + any.event_type);
    print("Sample event: " + any.event);
    print("Total docs: " + db.strategy_positions_historical.countDocuments());
  } else {
    print("Collection EMPTY");
  }
}

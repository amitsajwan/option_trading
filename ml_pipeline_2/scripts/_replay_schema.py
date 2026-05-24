import subprocess

script = """
use trading_ai
var d = db.strategy_positions_historical.findOne({trade_date_ist: {$gte: "2024-08-01", $lte: "2024-10-31"}});
if (d) {
  print("KEYS: " + Object.keys(d).join(", "));
  print("event_type: " + d.event_type);
  print("event: " + d.event);
  print("exit_reason: " + d.exit_reason);
  print("pnl_pct: " + d.pnl_pct);
  print("direction: " + d.direction);
  print("trade_date_ist: " + d.trade_date_ist);
  print("reason: " + (d.reason||"").toString().slice(0,100));
} else {
  print("NO DOCS FOUND in 2024-08-01 to 2024-10-31");
  var any = db.strategy_positions_historical.findOne();
  if (any) {
    print("Sample doc date: " + any.trade_date_ist);
    print("Sample event: " + any.event_type + " / " + any.event);
  } else {
    print("Collection is EMPTY");
  }
}
"""
result = subprocess.run(
    ["docker", "exec", "option_trading-mongo-1", "mongosh", "--quiet", "--eval", script],
    capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:300])

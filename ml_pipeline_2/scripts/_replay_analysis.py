import subprocess, json

script = """
use trading_ai

// Summary stats
var positions = db.strategy_positions_historical;
var from_date = "2024-08-01";
var to_date   = "2024-10-31";

var closed = positions.find({
  trade_date_ist: {$gte: from_date, $lte: to_date},
  event: "POSITION_CLOSE"
}).toArray();

var total = closed.length;
var wins = 0, losses = 0, timeouts = 0, stops = 0, targets = 0;
var total_pnl = 0;
var pe_count = 0, ce_count = 0;
var pe_pnl = 0, ce_pnl = 0;
var worst = 999, best = -999;
var worst_date = "", best_date = "";
var daily = {};

closed.forEach(function(t) {
  var pnl = t.pnl_pct || 0;
  total_pnl += pnl;
  if (pnl > 0) wins++; else losses++;
  var reason = (t.exit_reason || t.exitReason || "").toString();
  if (reason.includes("TARGET")) targets++;
  else if (reason.includes("STOP")) stops++;
  else timeouts++;

  var dir = (t.direction || "").toString().toUpperCase();
  if (dir === "PE") { pe_count++; pe_pnl += pnl; }
  else if (dir === "CE") { ce_count++; ce_pnl += pnl; }

  if (pnl < worst) { worst = pnl; worst_date = t.trade_date_ist; }
  if (pnl > best)  { best  = pnl; best_date  = t.trade_date_ist; }

  var d = t.trade_date_ist;
  if (!daily[d]) daily[d] = {pnl:0, trades:0, wins:0};
  daily[d].pnl    += pnl;
  daily[d].trades += 1;
  if (pnl > 0) daily[d].wins += 1;
});

var avg_pnl = total > 0 ? total_pnl / total : 0;
var win_rate = total > 0 ? (wins / total * 100).toFixed(1) : 0;

print("=== TRADE SUMMARY: " + from_date + " to " + to_date + " ===");
print("Total closed trades : " + total);
print("Wins / Losses       : " + wins + " / " + losses);
print("Win rate            : " + win_rate + "%");
print("Total PnL (sum pct) : " + total_pnl.toFixed(4));
print("Avg PnL per trade   : " + avg_pnl.toFixed(4));
print("Best trade          : " + best.toFixed(4) + " on " + best_date);
print("Worst trade         : " + worst.toFixed(4) + " on " + worst_date);
print("Exit reasons        : TARGET=" + targets + " STOP=" + stops + " TIMEOUT=" + timeouts);
print("PE trades           : " + pe_count + " pnl=" + pe_pnl.toFixed(4));
print("CE trades           : " + ce_count + " pnl=" + ce_pnl.toFixed(4));
print("");

// Daily breakdown (sorted)
print("=== DAILY BREAKDOWN ===");
var days = Object.keys(daily).sort();
days.forEach(function(d) {
  var dd = daily[d];
  var wr = (dd.wins/dd.trades*100).toFixed(0);
  var sign = dd.pnl >= 0 ? "+" : "";
  print(d + "  trades=" + dd.trades + "  pnl=" + sign + dd.pnl.toFixed(4) + "  wr=" + wr + "%");
});
"""

result = subprocess.run(
    ["docker", "exec", "option_trading-mongo-1", "mongosh", "--quiet", "--eval", script],
    capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:500])

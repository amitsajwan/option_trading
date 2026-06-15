// Clean LIVE trade ledger — strips replay/sim noise + dedups session restarts.
//
// Run:  docker exec option_trading-mongo-1 mongosh --quiet trading_ai /tmp/live_ledger.js
//
// "Genuine live" = closed positions from a live session: run_id is null (early
// June-1 book) OR matches ^paper- (the live paper sessions; format
// paper-YYYYMMDD-HHMMSS-hash). Everything ^sim- is replay and is EXCLUDED.
// Dedup: a mid-day restart re-records earlier trades under a new session id, so we
// collapse to one row per (trade_date, entry_time, strike, direction).
//
// NOTE: these are the live strategy's PAPER decisions. No real money filled in
// June 2026 (all Dhan orders rejected Invalid IP) — see the execution forensic.

var LIVE_MATCH = {
  event: "POSITION_CLOSE",
  $or: [{ run_id: null }, { run_id: { $regex: "^paper-" } }]
};

// 1) dedup to one row per physical trade
var deduped = db.strategy_positions.aggregate([
  { $match: LIVE_MATCH },
  { $group: {
      _id: { d: "$trade_date_ist", e: "$entry_time", k: "$strike", dir: "$direction" },
      pnl: { $first: "$pnl_pct" },
      exit: { $first: "$exit_reason" },
      bars: { $first: "$bars_held" },
      run_id: { $first: "$run_id" }
  }}
], { allowDiskUse: true }).toArray();

// 2) per-day summary
var byday = {};
deduped.forEach(function (t) {
  var d = t._id.d;
  byday[d] = byday[d] || { n: 0, wins: 0, net: 0, ce: 0, pe: 0 };
  var b = byday[d];
  b.n++; if (t.pnl > 0) b.wins++; b.net += (t.pnl || 0);
  if (t._id.dir === "CE") b.ce++; else if (t._id.dir === "PE") b.pe++;
});

print("=== CLEAN LIVE LEDGER (paper book, replay excluded, deduped) ===");
print("day          trades  wins  winrate  netPnl%   CE/PE");
var days = Object.keys(byday).sort();
var tot = { n: 0, wins: 0, net: 0 };
days.forEach(function (d) {
  var b = byday[d];
  tot.n += b.n; tot.wins += b.wins; tot.net += b.net;
  print("  " + d + "   " + String(b.n).padStart(4) + "  " + String(b.wins).padStart(4) +
        "   " + (b.n ? (100 * b.wins / b.n).toFixed(0) : "0").padStart(5) + "%  " +
        (b.net * 100).toFixed(2).padStart(8) + "   " + b.ce + "/" + b.pe);
});
print("  " + "-".repeat(52));
print("  TOTAL       " + String(tot.n).padStart(4) + "  " + String(tot.wins).padStart(4) +
      "   " + (tot.n ? (100 * tot.wins / tot.n).toFixed(0) : "0").padStart(5) + "%  " +
      (tot.net * 100).toFixed(2).padStart(8));
print("  avg net per trade: " + (tot.n ? (tot.net / tot.n * 100).toFixed(3) : "0") + "%  (PRE-cost; ~1% round-trip cost applies)");

print("\n=== trade detail ===");
deduped.sort(function (a, b) { return (a._id.d + a._id.e) < (b._id.d + b._id.e) ? -1 : 1; });
deduped.forEach(function (t) {
  print("  " + t._id.d + " " + String(t._id.e).substr(11, 8) + " " + t._id.dir +
        " K" + t._id.k + " pnl%=" + (t.pnl * 100).toFixed(1).padStart(6) + " " + t.exit);
});

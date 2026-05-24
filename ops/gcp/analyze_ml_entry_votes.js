// Analyze ML_ENTRY vote behavior for det_dir replay run.
const runId = "e8ba040a-a8dd-47d1-9bf8-ceffba85e809";
const votes = db.strategy_votes_historical.find(
  { run_id: runId, strategy: "ML_ENTRY", signal_type: "ENTRY" },
  { confidence: 1, timestamp: 1, trade_date_ist: 1, _id: 0 }
).toArray();
const probs = votes.map((v) => Number(v.confidence)).filter((x) => !isNaN(x));
probs.sort((a, b) => a - b);
const pct = (p) => (probs.length ? probs[Math.floor(probs.length * p)] : null);
print("ml_entry_votes", votes.length);
if (probs.length) {
  print("prob_min", probs[0], "p50", pct(0.5), "p90", pct(0.9), "max", probs[probs.length - 1]);
  print("above_0.50", probs.filter((x) => x >= 0.5).length);
  print("above_0.55", probs.filter((x) => x >= 0.55).length);
}
const signals = db.trade_signals_historical.countDocuments({
  run_id: runId,
  signal_type: "ENTRY",
  entry_strategy_name: { $ne: "ML_ENTRY" },
});
const closed = db.strategy_positions_historical.countDocuments({
  run_id: runId,
  event: "POSITION_CLOSE",
});
print("rule_entry_signals", signals);
print("closed_trades", closed);
const byDay = db.strategy_votes_historical.aggregate([
  { $match: { run_id: runId, strategy: "ML_ENTRY" } },
  { $group: { _id: "$trade_date_ist", n: { $sum: 1 }, avg: { $avg: "$confidence" } } },
  { $sort: { _id: 1 } },
  { $limit: 15 },
]);
print("votes_by_day_sample:");
byDay.forEach((r) => print(r._id, "n=" + r.n, "avg_prob=" + (r.avg || 0).toFixed(3)));

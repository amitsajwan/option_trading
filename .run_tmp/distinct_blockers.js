var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var result = db.strategy_decision_traces_sim.aggregate([
  { $match: { run_id: rid } },
  { $group: { _id: "$primary_blocker_gate", count: { $sum: 1 } } },
  { $sort: { count: -1 } }
]).toArray();

result.forEach(function(r) {
  print(r._id + ": " + r.count);
});

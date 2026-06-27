var db = db.getSiblingDB('trading_ai');
var rid = "02cd304b-db1b-438b-a601-c645f8902b35";

var votes = db.strategy_votes_sim.countDocuments({run_id: rid});
var entryVotes = db.strategy_votes_sim.countDocuments({run_id: rid, signal_type: "ENTRY"});
var signals = db.trade_signals_sim.countDocuments({run_id: rid});
var pos = db.strategy_positions_sim.countDocuments({run_id: rid});

print("Run: " + rid);
print("  Votes: " + votes);
print("  Entry votes: " + entryVotes);
print("  Trade signals: " + signals);
print("  Positions: " + pos);

if (entryVotes > 0) {
    var sample = db.strategy_votes_sim.findOne({run_id: rid, signal_type: "ENTRY"});
    print("  Sample entry confidence: " + sample.confidence);
    print("  Sample entry strategy: " + sample.strategy_name);
    print("  Sample entry reason: " + (sample.reason || "").substring(0, 60));
}

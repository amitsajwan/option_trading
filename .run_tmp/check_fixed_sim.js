var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

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
    print("  Sample entry reason: " + (sample.reason || "").substring(0, 70));
}

if (signals > 0) {
    var sig = db.trade_signals_sim.findOne({run_id: rid});
    print("  Sample signal direction: " + sig.direction);
    print("  Sample signal confidence: " + sig.confidence);
}

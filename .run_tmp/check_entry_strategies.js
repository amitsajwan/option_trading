var db = db.getSiblingDB('trading_ai');
var rid = "a7729382-daf9-4666-94a0-fd7e94897618";

var entryVotes = db.strategy_votes_sim.find({run_id: rid, signal_type: "ENTRY"}).toArray();
print("Total entry votes: " + entryVotes.length);

var strategies = {};
entryVotes.forEach(function(v) {
    var s = v.strategy_name || "undefined";
    strategies[s] = (strategies[s] || 0) + 1;
});

print("Entry votes by strategy:");
for (var s in strategies) {
    print("  " + s + ": " + strategies[s]);
}

// Also check if ML_ENTRY is in the votes at all (not just entry)
var mlVotes = db.strategy_votes_sim.countDocuments({run_id: rid, strategy_name: "ML_ENTRY"});
print("Total ML_ENTRY votes (any signal_type): " + mlVotes);

var volGateVotes = db.strategy_votes_sim.countDocuments({run_id: rid, strategy_name: "VOL_GATE_ENTRY"});
print("Total VOL_GATE_ENTRY votes: " + volGateVotes);

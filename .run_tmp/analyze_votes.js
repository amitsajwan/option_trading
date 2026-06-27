var db = db.getSiblingDB('trading_ai');

// Check votes for one run
var rid = "a7729382-daf9-4666-94a0-fd7e94897618";
var votes = db.strategy_votes_sim.find({run_id: rid}).toArray();

print("Total votes: " + votes.length);

var entryVotes = votes.filter(v => v.signal_type === "ENTRY");
print("Entry votes: " + entryVotes.length);

if (entryVotes.length > 0) {
    var confidences = entryVotes.map(v => v.confidence);
    var minConf = Math.min(...confidences);
    var maxConf = Math.max(...confidences);
    var avgConf = confidences.reduce((a,b)=>a+b,0)/confidences.length;
    print("Entry confidence min=" + minConf + " max=" + maxConf + " avg=" + avgConf.toFixed(3));
    
    // Count by strategy
    var strategies = {};
    entryVotes.forEach(v => {
        var s = v.strategy_name || "unknown";
        strategies[s] = (strategies[s] || 0) + 1;
    });
    print("Entry votes by strategy:");
    for (var s in strategies) {
        print("  " + s + ": " + strategies[s]);
    }
    
    // Show first few entry votes
    print("Sample entry votes:");
    for (var i = 0; i < Math.min(5, entryVotes.length); i++) {
        var v = entryVotes[i];
        print("  " + v.direction + " conf=" + v.confidence + " strat=" + v.strategy_name + " reason=" + (v.reason || "").substring(0,60));
    }
}

// Check if any signals exist at all in trade_signals_sim
var allSignals = db.trade_signals_sim.countDocuments({});
print("Total signals in trade_signals_sim: " + allSignals);

// Check positions
var allPos = db.strategy_positions_sim.countDocuments({});
print("Total positions in strategy_positions_sim: " + allPos);

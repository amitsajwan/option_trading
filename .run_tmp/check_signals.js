var db = db.getSiblingDB('trading_ai');

// Show sample signal doc structure
var sig = db.trade_signals_sim.findOne({});
print("Sample signal doc keys: " + Object.keys(sig).join(", "));
print("run_id field: " + (sig.run_id || sig.sim_run_id || sig._run_id || "NOT FOUND"));

// Count by run_id
var rid = "a7729382-daf9-4666-94a0-fd7e94897618";
print("Signals with run_id='" + rid + "': " + db.trade_signals_sim.countDocuments({run_id: rid}));
print("Signals with run_id starting with 'a772': " + db.trade_signals_sim.countDocuments({run_id: {$regex: /^a772/}}));

// Show all distinct run_ids in signals
var distinctRids = db.trade_signals_sim.distinct("run_id");
print("Distinct run_ids in trade_signals_sim: " + distinctRids.length);
for (var i = 0; i < Math.min(10, distinctRids.length); i++) {
    var count = db.trade_signals_sim.countDocuments({run_id: distinctRids[i]});
    print("  " + distinctRids[i] + ": " + count);
}

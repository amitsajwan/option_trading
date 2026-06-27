var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var traces = db.strategy_decision_traces_sim.find({run_id: rid}).toArray();
print("Total decision traces: " + traces.length);

var blockers = {};
var samples = {};
traces.forEach(function(t) {
    var b = t.blocker || t.entry_blocker || (t.decision && t.decision.blocker) || "none";
    if (b && b !== "none" && b !== null) {
        blockers[b] = (blockers[b] || 0) + 1;
        if (!samples[b]) {
            samples[b] = {
                snapshot_id: t.snapshot_id,
                regime: t.regime,
                reason: (t.decision && t.decision.reason) || t.reason || ""
            };
        }
    }
});

print("\n=== Blocker counts ===");
var sorted = Object.entries(blockers).sort((a,b) => b[1]-a[1]);
sorted.forEach(([k,v]) => {
    print(k + ": " + v);
    var s = samples[k];
    if (s) {
        print("  sample: snapshot=" + s.snapshot_id + " regime=" + s.regime + " reason=" + s.reason.substring(0,60));
    }
});

var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var t = db.strategy_decision_traces_sim.findOne({run_id: rid, primary_blocker_gate: "no_selection"});
if (t && t.payload && t.payload.trace) {
    print("trace keys: " + Object.keys(t.payload.trace).join(", "));
    if (t.payload.trace.candidates) {
        t.payload.trace.candidates.forEach(function(c, i) {
            print("=== Candidate " + i + " ===");
            print(JSON.stringify(c, null, 2));
        });
    }
    if (t.payload.trace.votes) {
        print("=== votes ===");
        t.payload.trace.votes.forEach(function(v, i) {
            print("Vote " + i + ": strategy=" + v.strategy_name + " conf=" + v.confidence + " dir=" + v.direction);
        });
    }
}

var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var t = db.strategy_decision_traces_sim.findOne({run_id: rid, primary_blocker_gate: "no_selection"});
if (t && t.payload && t.payload.trace && t.payload.trace.candidates) {
    t.payload.trace.candidates.forEach(function(c, i) {
        print("Candidate " + i + ":");
        print("  strategy: " + c.strategy_name);
        print("  confidence: " + c.confidence);
        print("  direction: " + c.direction);
        print("  terminal_gate: " + c.terminal_gate_id);
        print("  terminal_status: " + c.terminal_status);
        print("  policy_allowed: " + (c.policy && c.policy.allowed));
        print("  policy_reason: " + (c.policy && c.policy.reason));
    });
} else {
    print("No candidates found");
}

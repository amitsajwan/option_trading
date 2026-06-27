var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var t = db.strategy_decision_traces_sim.findOne({run_id: rid, primary_blocker_gate: "no_selection"});
if (t && t.payload && t.payload.trace) {
    print("gates: " + JSON.stringify(t.payload.trace.gates));
    print("last_entry_trace: " + JSON.stringify(t.payload.trace.last_entry_trace));
}

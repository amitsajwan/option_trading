var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var t = db.strategy_decision_traces_sim.findOne({run_id: rid, primary_blocker_gate: "no_selection"});
if (t) {
    print("snapshot_id: " + t.snapshot_id);
    print("market_time: " + t.market_time_ist);
    print("regime: " + t.regime);
    print("vote_count: " + t.candidate_count);
    print("blocked_count: " + t.blocked_candidate_count);
    print("payload: " + JSON.stringify(t.payload, null, 2).substring(0, 1500));
} else {
    print("no no_selection trace found");
}

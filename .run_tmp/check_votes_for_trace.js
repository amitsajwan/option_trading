var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var t = db.strategy_decision_traces_sim.findOne({run_id: rid, primary_blocker_gate: "no_selection"});
if (t) {
    var sid = t.snapshot_id;
    print("Snapshot: " + sid);
    print("Trace votes: " + (t.payload.trace.votes ? t.payload.trace.votes.length : 0));
    
    var dbVotes = db.strategy_votes_sim.find({run_id: rid, snapshot_id: sid}).toArray();
    print("DB votes: " + dbVotes.length);
    dbVotes.forEach(function(v) {
        print("  strategy=" + v.strategy_name + " type=" + v.signal_type + " dir=" + v.direction + " conf=" + v.confidence);
        if (v.raw_signals) {
            print("    _policy_allowed=" + v.raw_signals._policy_allowed);
            print("    _entry_policy_mode=" + v.raw_signals._entry_policy_mode);
        }
    });
}

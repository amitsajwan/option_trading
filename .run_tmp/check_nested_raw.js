var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var v = db.strategy_votes_sim.findOne({run_id: rid, snapshot_id: "20260601_1002"});
if (v && v.payload && v.payload.vote && v.payload.vote.raw_signals) {
    var raw = v.payload.vote.raw_signals;
    print("_policy_allowed: " + raw._policy_allowed);
    print("_policy_reason: " + raw._policy_reason);
    print("_entry_policy_mode: " + raw._entry_policy_mode);
    print("proposed_strike: " + v.payload.vote.proposed_strike);
    print("proposed_entry_premium: " + v.payload.vote.proposed_entry_premium);
} else {
    print("No nested raw_signals found");
}

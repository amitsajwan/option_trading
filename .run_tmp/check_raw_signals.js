var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var v = db.strategy_votes_sim.findOne({run_id: rid, snapshot_id: "20260601_1002"});
if (v) {
    print("strategy_name: " + v.strategy_name);
    print("raw_signals keys: " + Object.keys(v.raw_signals || {}).join(", "));
    if (v.raw_signals) {
        print("_policy_allowed: " + v.raw_signals._policy_allowed);
        print("_policy_reason: " + v.raw_signals._policy_reason);
        print("_entry_policy_mode: " + v.raw_signals._entry_policy_mode);
    }
} else {
    print("No vote found");
}

var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var v = db.strategy_votes_sim.findOne({run_id: rid, snapshot_id: "20260601_1002"});
if (v) {
    print("Has proposed_strike: " + ("proposed_strike" in v));
    print("Has proposed_entry_premium: " + ("proposed_entry_premium" in v));
    print("proposed_strike value: " + v.proposed_strike);
    print("proposed_entry_premium value: " + v.proposed_entry_premium);
} else {
    print("No vote found");
}

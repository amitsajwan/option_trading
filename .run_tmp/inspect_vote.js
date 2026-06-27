var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var vote = db.strategy_votes_sim.findOne({run_id: rid, signal_type: "ENTRY"});
print(JSON.stringify(vote, null, 2));

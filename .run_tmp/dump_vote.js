var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var v = db.strategy_votes_sim.findOne({run_id: rid, snapshot_id: "20260601_1002"});
print(JSON.stringify(v, null, 2));

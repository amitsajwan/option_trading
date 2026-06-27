var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var t = db.strategy_decision_traces_sim.findOne({run_id: rid});
print(JSON.stringify(t, null, 2));

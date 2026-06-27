var db = db.getSiblingDB('trading_ai');
var rid = "15991f2f-571c-4999-8888-8ef3667016f3";

var noSelTraces = db.strategy_decision_traces_sim.find({run_id: rid, primary_blocker_gate: "no_selection"}).limit(10).toArray();
var missingStrike = 0;
var missingPremium = 0;
var bothMissing = 0;

noSelTraces.forEach(function(t) {
    var sid = t.snapshot_id;
    var v = db.strategy_votes_sim.findOne({run_id: rid, snapshot_id: sid, signal_type: "ENTRY"});
    if (v) {
        if (!v.proposed_strike) missingStrike++;
        if (!v.proposed_entry_premium) missingPremium++;
        if (!v.proposed_strike && !v.proposed_entry_premium) bothMissing++;
    }
});

print("no_selection traces checked: " + noSelTraces.length);
print("Missing proposed_strike: " + missingStrike);
print("Missing proposed_entry_premium: " + missingPremium);
print("Both missing: " + bothMissing);

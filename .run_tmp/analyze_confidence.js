var db = db.getSiblingDB('trading_ai');
var rid = "a7729382-daf9-4666-94a0-fd7e94897618";

var entryVotes = db.strategy_votes_sim.find({run_id: rid, signal_type: "ENTRY"}).toArray();

var confidences = entryVotes.map(function(v) { return v.confidence || 0; });
confidences.sort(function(a,b){return a-b;});

print("Entry vote confidence distribution (n=" + confidences.length + "):");
print("  min: " + confidences[0]);
print("  p10: " + confidences[Math.floor(confidences.length * 0.10)]);
print("  p25: " + confidences[Math.floor(confidences.length * 0.25)]);
print("  p50: " + confidences[Math.floor(confidences.length * 0.50)]);
print("  p75: " + confidences[Math.floor(confidences.length * 0.75)]);
print("  p90: " + confidences[Math.floor(confidences.length * 0.90)]);
print("  max: " + confidences[confidences.length - 1]);

var above65 = confidences.filter(function(c){ return c >= 0.65; }).length;
print("  >= 0.65 (min_confidence): " + above65);

// Show sample reasons for highest-confidence votes
print("\nHighest confidence entry votes:");
var sorted = entryVotes.slice().sort(function(a,b){ return (b.confidence||0) - (a.confidence||0); });
for (var i = 0; i < Math.min(5, sorted.length); i++) {
    var v = sorted[i];
    print("  conf=" + v.confidence + " dir=" + v.direction + " reason=" + (v.reason||"").substring(0,70));
}

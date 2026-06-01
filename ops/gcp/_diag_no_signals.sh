#!/usr/bin/env bash
cd /opt/option_trading

echo "=== Decision traces — real schema (final_outcome, primary_blocker_gate) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").strategy_decision_traces;
const today = "2026-05-26";
print("--- final_outcome distribution ---");
c.aggregate([
  {$match:{trade_date_ist:today}},
  {$group:{_id:"$final_outcome", n:{$sum:1}}},
  {$sort:{n:-1}}
]).toArray().forEach(b => print("  ", b.n, ":", b._id));

print("");
print("--- primary_blocker_gate distribution ---");
c.aggregate([
  {$match:{trade_date_ist:today}},
  {$group:{_id:"$primary_blocker_gate", n:{$sum:1}}},
  {$sort:{n:-1}}
]).toArray().forEach(b => print("  ", b.n, ":", b._id));

print("");
print("--- engine_mode / decision_mode / evaluation_type ---");
c.aggregate([
  {$match:{trade_date_ist:today}},
  {$group:{_id:{engine:"$engine_mode", decision:"$decision_mode", eval:"$evaluation_type"}, n:{$sum:1}}},
  {$sort:{n:-1}}
]).toArray().forEach(b => print("  ", b.n, ":", JSON.stringify(b._id)));

print("");
print("--- candidate_count vs blocked_candidate_count ---");
c.aggregate([
  {$match:{trade_date_ist:today}},
  {$group:{_id:{c:"$candidate_count", b:"$blocked_candidate_count"}, n:{$sum:1}}},
  {$sort:{n:-1}}
]).toArray().forEach(b => print("  ", b.n, ":", JSON.stringify(b._id)));

print("");
print("--- time distribution of evaluations ---");
c.aggregate([
  {$match:{trade_date_ist:today}},
  {$project:{hour:{$substr:["$market_time_ist", 0, 2]}}},
  {$group:{_id:"$hour", n:{$sum:1}}},
  {$sort:{_id:1}}
]).toArray().forEach(b => print("  ", b._id, "h:", b.n));
'

echo ""
echo "=== One full decision trace (the most recent) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").strategy_decision_traces;
const d = c.find({trade_date_ist:"2026-05-26"}).sort({_id:-1}).limit(1).toArray()[0];
if (d) {
  print(JSON.stringify(d, null, 2).substring(0, 3500));
}
'

echo ""
echo "=== Snapshot regime detection — is regime being set? ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
const today = "2026-05-26";
// regime is under payload.snapshot.session_context.regime
const regimes = c.aggregate([
  {$match:{trade_date_ist:today}},
  {$project:{regime:"$payload.snapshot.session_context.regime", vix_regime:"$payload.snapshot.vix_context.vix_regime"}},
  {$group:{_id:{r:"$regime", vix_r:"$vix_regime"}, n:{$sum:1}}},
  {$sort:{n:-1}}
]).toArray();
print("regime distribution across 294 snapshots:");
regimes.forEach(b => print("  ", b.n, " : ", JSON.stringify(b._id)));
'

echo ""
echo "=== Strategy_persistence errors (3 reported) ==="
sudo docker compose logs strategy_persistence_app 2>&1 | grep -iE "error|exception|traceback" | head -20

echo ""
echo "=== Strategy_app errors/warnings today ==="
sudo docker compose logs --since=8h strategy_app 2>&1 | grep -iE "error|warn|skip|reject|halt|paused" | grep -v "ignored non-json" | tail -20

echo ""
echo "=== Strategy votes — what was emitted? ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").strategy_votes;
const today = "2026-05-26";
print("--- vote.direction distribution ---");
c.aggregate([
  {$match:{trade_date_ist:today}},
  {$group:{_id:"$direction", n:{$sum:1}}}
]).toArray().forEach(b => print("  ", b.n, ":", b._id));
print("");
print("--- one sample vote ---");
const v = c.find({trade_date_ist:today}).sort({_id:-1}).limit(1).toArray()[0];
if (v) print(JSON.stringify(v, null, 2).substring(0, 1500));
'

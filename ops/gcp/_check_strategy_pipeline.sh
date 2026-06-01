#!/usr/bin/env bash
cd /opt/option_trading

echo "=== strategy_app: consumption + decisions ==="
sudo docker compose exec -T strategy_app cat /app/.run/strategy_app/runtime_state.json 2>/dev/null | head -50

echo ""
echo "=== strategy_app: votes JSONL tail ==="
sudo docker compose exec -T strategy_app sh -c "ls -la /app/.run/strategy_app/*.jsonl 2>/dev/null"
echo "--- last 5 votes ---"
sudo docker compose exec -T strategy_app sh -c "tail -5 /app/.run/strategy_app/votes.jsonl 2>/dev/null || echo '(no votes.jsonl yet)'"
echo "--- last 5 decisions ---"
sudo docker compose exec -T strategy_app sh -c "tail -5 /app/.run/strategy_app/decisions.jsonl 2>/dev/null || echo '(no decisions.jsonl yet)'"

echo ""
echo "=== strategy_app: 'consumed' health logs (last 5 min) ==="
sudo docker compose logs --since=5m strategy_app 2>&1 | grep -E "consumed|published|engine|vote|signal|position|decision" | tail -20

echo ""
echo "=== Mongo: live decision_traces (today only) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").strategy_decision_traces;
const today = c.countDocuments({trade_date_ist: "2026-05-26"});
print("decisions today:", today);
if (today > 0) {
  const latest = c.find({trade_date_ist:"2026-05-26"}).sort({_id:-1}).limit(1).toArray()[0];
  print("latest decision _id:", latest._id);
  print("latest market_time_ist:", latest.market_time_ist);
  print("verdict:", latest.verdict || latest.outcome);
  print("decision keys:", Object.keys(latest).slice(0,15).join(","));
}
print("");
const votes = db.getSiblingDB("trading_ai").strategy_votes;
print("votes today:", votes.countDocuments({trade_date_ist:"2026-05-26"}));
'

echo ""
echo "=== Mongo: votes today ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").strategy_votes;
const today = c.countDocuments({trade_date_ist: "2026-05-26"});
print("votes today:", today);
if (today > 0) {
  const latest = c.find({trade_date_ist:"2026-05-26"}).sort({_id:-1}).limit(1).toArray()[0];
  print("latest vote ts:", latest.market_time_ist);
  print("vote keys:", Object.keys(latest).slice(0,20).join(","));
}
'

echo ""
echo "=== persistence_app health ==="
sudo docker compose logs --since=2m persistence_app 2>&1 | grep -E "consumed|written" | tail -5
echo "--- strategy_persistence_app health ---"
sudo docker compose logs --since=2m strategy_persistence_app 2>&1 | grep -E "consumed|written|published" | tail -5

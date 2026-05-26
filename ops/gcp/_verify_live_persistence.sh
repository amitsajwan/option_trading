#!/usr/bin/env bash
# Verify all live data persistence layers on VM
cd /opt/option_trading

echo "================================================="
echo "1. SNAPSHOTS — Mongo trading_ai.phase1_market_snapshots"
echo "================================================="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
print("total live snapshots:", c.countDocuments({}));
const latest = c.find({}).sort({_id:-1}).limit(1).toArray()[0];
if (latest) {
  print("latest _id:", latest._id);
  print("keys:", Object.keys(latest).slice(0,15).join(","));
  print("instrument_symbol:", latest.instrument_symbol);
  print("ist_timestamp:", latest.ist_timestamp);
  print("last_price:", latest.last_price);
  print("execution_mode:", latest.execution_mode);
}
'

echo ""
echo "================================================="
echo "2. OPTIONS CHAIN — Redis live:options:*"
echo "================================================="
echo "--- keys ---"
sudo docker compose exec -T redis redis-cli KEYS 'live:options:*'
echo "--- chain TTL ---"
sudo docker compose exec -T redis redis-cli TTL 'live:options:BANKNIFTY26JUNFUT:chain'
echo "--- chain size (bytes) ---"
sudo docker compose exec -T redis redis-cli STRLEN 'live:options:BANKNIFTY26JUNFUT:chain'
echo "--- options in Mongo (any collection) ---"
sudo docker compose exec -T mongo mongosh --quiet --eval '
const db_ = db.getSiblingDB("trading_ai");
db_.getCollectionNames().filter(n => n.match(/option|chain/i)).forEach(n => {
  print(n, ":", db_.getCollection(n).countDocuments({}));
});
print("(check whether options chain is persisted to Mongo or only Redis)");
'

echo ""
echo "================================================="
echo "3. DEPTH PRICES — Redis live:depth:*"
echo "================================================="
echo "--- depth CE ---"
sudo docker compose exec -T redis redis-cli GET 'live:depth:atm_ce:latest'
echo "--- depth PE ---"
sudo docker compose exec -T redis redis-cli GET 'live:depth:atm_pe:latest'
echo "--- depth TTLs ---"
sudo docker compose exec -T redis redis-cli TTL 'live:depth:atm_ce:latest'
sudo docker compose exec -T redis redis-cli TTL 'live:depth:atm_pe:latest'
echo "--- depth persisted to Mongo? ---"
sudo docker compose exec -T mongo mongosh --quiet --eval '
const db_ = db.getSiblingDB("trading_ai");
db_.getCollectionNames().filter(n => n.match(/depth/i)).forEach(n => {
  print(n, ":", db_.getCollection(n).countDocuments({}));
});
print("(if no depth collection appears, depth is Redis-only by design)");
'

echo ""
echo "================================================="
echo "4. OHLC BARS — Redis live:ohlc_sorted:*"
echo "================================================="
echo "--- BANKNIFTY26JUNFUT 1m bar count ---"
sudo docker compose exec -T redis redis-cli ZCARD 'live:ohlc_sorted:BANKNIFTY26JUNFUT:1m'
echo "--- latest 2 bars ---"
sudo docker compose exec -T redis redis-cli ZREVRANGE 'live:ohlc_sorted:BANKNIFTY26JUNFUT:1m' 0 1

echo ""
echo "================================================="
echo "5. STRATEGY OUTPUTS — Mongo + JSONL"
echo "================================================="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const db_ = db.getSiblingDB("trading_ai");
print("strategy_votes:", db_.strategy_votes.countDocuments({}));
print("strategy_positions:", db_.strategy_positions.countDocuments({}));
print("trade_signals:", db_.trade_signals.countDocuments({}));
print("strategy_decision_traces:", db_.strategy_decision_traces.countDocuments({}));
'
echo "--- strategy_app JSONL files ---"
sudo docker compose exec -T strategy_app ls -la /app/.run/strategy_app/ 2>&1 | tail -10

echo ""
echo "================================================="
echo "6. INGESTION HEALTH — websocket tick + VIX"
echo "================================================="
echo "--- ingestion_app health ---"
sudo docker compose exec -T ingestion_app curl -s http://localhost:8004/health 2>&1 | head -3
echo "--- VIX tick ---"
sudo docker compose exec -T redis redis-cli GET 'live:websocket:tick:INDIAVIX:latest'

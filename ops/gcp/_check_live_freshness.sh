#!/usr/bin/env bash
cd /opt/option_trading
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')
NOW_IST=$(TZ='Asia/Kolkata' date '+%Y-%m-%d %H:%M:%S IST')
echo "VM clock now: $NOW"
echo "VM clock IST: $NOW_IST"
echo ""

echo "=== Latest OHLC bar timestamp (Redis) ==="
sudo docker compose exec -T redis redis-cli ZREVRANGE 'live:ohlc_sorted:BANKNIFTY26JUNFUT:1m' 0 0 WITHSCORES

echo ""
echo "=== Bar count in last 5 minutes (Redis ZRANGEBYSCORE) ==="
NOW_EPOCH=$(date +%s)
FIVE_MIN_AGO=$((NOW_EPOCH - 300))
sudo docker compose exec -T redis redis-cli ZCOUNT 'live:ohlc_sorted:BANKNIFTY26JUNFUT:1m' $FIVE_MIN_AGO $NOW_EPOCH

echo ""
echo "=== Latest snapshot timestamp (Mongo) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
const doc = c.find({}).sort({_id:-1}).limit(1).toArray()[0];
if (doc) {
  print("total:", c.countDocuments({}));
  print("latest _id ts:", doc._id.getTimestamp());
  print("market_time_ist:", doc.market_time_ist);
  print("trade_date:", doc.trade_date_ist);
}
'

echo ""
echo "=== Last 5 minutes of snapshot_app logs ==="
sudo docker compose logs --since=5m snapshot_app 2>&1 | tail -10

echo ""
echo "=== Last 5 minutes of ingestion_app logs ==="
sudo docker compose logs --since=5m ingestion_app 2>&1 | tail -15

echo ""
echo "=== Dashboard logs (WebSocket activity) ==="
sudo docker compose logs --since=2m dashboard 2>&1 | tail -15

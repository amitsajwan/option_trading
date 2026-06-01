#!/usr/bin/env bash
cd /opt/option_trading
NOW_IST=$(TZ='Asia/Kolkata' date '+%H:%M:%S')
echo "VM time IST: $NOW_IST"

echo ""
echo "=== Dashboard container image + uptime ==="
sudo docker compose ps dashboard
echo ""
echo "--- dashboard image SHA ---"
sudo docker inspect option_trading-dashboard-1 --format '{{.Image}}' 2>&1
sudo docker inspect option_trading-dashboard-1 --format 'Image tag: {{.Config.Image}}' 2>&1

echo ""
echo "=== Was dashboard rebuilt for 53b95e9? (commit on VM HEAD) ==="
sudo -u $(whoami) git -C /opt/option_trading log --oneline -1 2>&1
sudo cat /opt/option_trading/.git/HEAD 2>&1

echo ""
echo "=== Dashboard recent logs (5 min) — looking for WebSocket frames ==="
sudo docker compose logs --since=5m dashboard 2>&1 | tail -30

echo ""
echo "=== Test: dashboard /api/health and live endpoints ==="
sudo docker compose exec -T dashboard sh -c "wget -qO- http://localhost:8008/api/health || echo no-wget" 2>&1 | head -5

echo ""
echo "=== Check if there's a snapshot pub/sub subscriber for the dashboard ==="
sudo docker compose exec -T redis redis-cli PUBSUB NUMSUB \
  market:snapshot:v1 \
  market:strategy:votes:v1 \
  market:strategy:positions:v1 \
  market:strategy:signals:v1 \
  market:strategy:decision_trace:v1

echo ""
echo "=== Snapshot freshness right now ==="
sudo docker compose exec -T redis redis-cli ZREVRANGE 'live:ohlc_sorted:BANKNIFTY26JUNFUT:1m' 0 0
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
const doc = c.find({}).sort({_id:-1}).limit(1).toArray()[0];
if (doc) print("latest snapshot market_time_ist:", doc.market_time_ist, " count:", c.countDocuments({}));
'

echo ""
echo "=== Dashboard /api/live/snapshots or similar endpoint ==="
# try a few likely endpoint shapes
for ep in "/api/live/snapshot" "/api/snapshot/latest" "/api/live" "/api/ohlc/BANKNIFTY26JUNFUT" "/api/market/state"; do
  echo "--- $ep ---"
  sudo docker compose exec -T dashboard sh -c "wget -qO- 'http://localhost:8008$ep' 2>/dev/null | head -c 200 ; echo" 2>&1
done

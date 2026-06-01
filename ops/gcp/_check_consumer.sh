#!/usr/bin/env bash
cd /opt/option_trading

echo "=== strategy_app consumer lock state ==="
sudo docker compose exec -T redis redis-cli GET 'strategy_app:consumer_lock:market:snapshot:v1'
sudo docker compose exec -T redis redis-cli TTL 'strategy_app:consumer_lock:market:snapshot:v1'

echo ""
echo "=== Redis pubsub channel subscribers ==="
sudo docker compose exec -T redis redis-cli PUBSUB CHANNELS '*'
echo ""
echo "=== Pubsub NUMSUB for snapshot topic ==="
sudo docker compose exec -T redis redis-cli PUBSUB NUMSUB 'market:snapshot:v1' 'market:strategy:votes:v1'

echo ""
echo "=== strategy_persistence_app what topic does it subscribe to? ==="
sudo docker compose exec -T strategy_persistence_app env 2>&1 | grep -iE 'topic|subscribe' | head -10

echo ""
echo "=== strategy_app process tree ==="
sudo docker compose exec -T strategy_app ps -ef 2>&1 | tail -10

echo ""
echo "=== Manually publish a test snapshot to topic ==="
sudo docker compose exec -T redis redis-cli LRANGE 'market:snapshot:v1' 0 -1 2>&1 | head -3
echo "--- snapshot_app actually publishes via pub/sub (not list). Check by injecting probe ---"

echo ""
echo "=== Send a test message and see if strategy_app logs anything ==="
TS=$(date +%s)
sudo docker compose exec -T redis redis-cli PUBLISH 'market:snapshot:v1' "PROBE_$TS"
sleep 3
echo "--- strategy_app log lines mentioning PROBE ---"
sudo docker compose logs --since=10s strategy_app 2>&1 | tail -10

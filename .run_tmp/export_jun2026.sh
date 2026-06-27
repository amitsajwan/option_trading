#!/bin/bash
set -e
echo "Exporting June-2026 snapshots from MongoDB..."
sudo docker exec option_trading-mongo-1 mongoexport \
  -d trading_ai \
  -c phase1_market_snapshots \
  --query '{"trade_date_ist":{"$gte":"2026-06-01"}}' \
  --fields "snapshot_id,trade_date_ist,timestamp,payload" \
  --out /tmp/jun2026_snaps.json

echo "Copying from container to host..."
sudo docker cp option_trading-mongo-1:/tmp/jun2026_snaps.json /tmp/jun2026_snaps.json
ls -lh /tmp/jun2026_snaps.json
echo "Done."

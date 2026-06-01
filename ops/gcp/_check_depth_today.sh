#!/usr/bin/env bash
cd /opt/option_trading

echo "=== market_depth_ticks collection (the NEW one) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").market_depth_ticks;
print("total docs:", c.countDocuments({}));
print("docs for today:", c.countDocuments({trade_date_ist:"2026-05-26"}));
const first = c.find({}).sort({_id:1}).limit(1).toArray()[0];
const last  = c.find({}).sort({_id:-1}).limit(1).toArray()[0];
if (first) print("first :", first.fetched_at_ist, " ", first.instrument);
if (last)  print("last  :", last.fetched_at_ist, " ", last.instrument);
'

echo ""
echo "=== Any Mongo collection with 'depth' or 'tick' in its name ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
db.getSiblingDB("trading_ai").getCollectionNames()
  .filter(n => n.match(/depth|tick/i))
  .forEach(n => print("  ", n));
'

echo ""
echo "=== Redis depth keys (now, post-market) ==="
sudo docker compose exec -T redis redis-cli KEYS 'live:depth:*'
sudo docker compose exec -T redis redis-cli KEYS '*:depth:*'

echo ""
echo "=== What WAS captured today: option chain inside snapshots (294 docs) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
print("snapshots today:", c.countDocuments({trade_date_ist:"2026-05-26"}));
const last = c.find({trade_date_ist:"2026-05-26"}).sort({_id:-1}).limit(1).toArray()[0];
if (last) {
  const snap = (last.payload || {}).snapshot || {};
  print("strikes captured per snapshot:", (snap.strikes || []).length);
  print("ATM CE/PE OI/volume present?", snap.atm_options !== undefined);
  print("chain_aggregates present?    ", snap.chain_aggregates !== undefined);
}
print("");
print("Per-snapshot we have:");
print("  - ATM CE LTP, OI, OI_change, volume, IV, vol_ratio");
print("  - ATM PE LTP, OI, OI_change, volume, IV, vol_ratio");
print("  - 25 strikes around ATM: LTP, OI, volume per leg");
print("  - chain_aggregates: PCR, max_pain, total OI/vol, straddle price");
print("  - futures OHLCV, VIX, regime context");
print("");
print("Per-snapshot we DO NOT have (depth_collector did not write to Mongo today):");
print("  - 5-level bid/ask ladder (qty, orders) per strike");
print("  - 5-second granular ticks (snapshots are 1-min)");
print("  - microprice / spread / qty_imbalance");
'

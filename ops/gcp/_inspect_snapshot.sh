#!/usr/bin/env bash
cd /opt/option_trading

echo "=== Latest snapshot payload (top-level keys) ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
const doc = c.find({}).sort({_id:-1}).limit(1).toArray()[0];
print("event_type:", doc.event_type);
print("instrument:", doc.instrument);
print("market_time_ist:", doc.market_time_ist);
print("trade_date_ist:", doc.trade_date_ist);
print("source:", doc.source);
print("payload keys:", Object.keys(doc.payload).slice(0, 30).join(","));
print("");
const snap = doc.payload.snapshot || {};
print("--- snapshot keys ---");
print(Object.keys(snap).slice(0, 40).join(","));
print("");
print("--- atm_options (option data) ---");
print(JSON.stringify(snap.atm_options || null).substring(0, 800));
print("");
print("--- chain_aggregates ---");
print(JSON.stringify(snap.chain_aggregates || null).substring(0, 500));
print("");
print("--- strikes count ---");
print("strikes:", (snap.strikes || []).length || "(not array)");
if (snap.strikes && snap.strikes.length > 0) {
  print("first strike:", JSON.stringify(snap.strikes[0]).substring(0, 400));
}
print("");
print("--- futures_bar (price) ---");
print(JSON.stringify(snap.futures_bar || null).substring(0, 300));
print("");
print("--- vix_context ---");
print(JSON.stringify(snap.vix_context || null).substring(0, 200));
'

echo ""
echo "=== Snapshot publication rate ==="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
const cnt = c.countDocuments({});
const first = c.find({}).sort({_id:1}).limit(1).toArray()[0];
const last = c.find({}).sort({_id:-1}).limit(1).toArray()[0];
print("total:", cnt);
if (first && last) {
  print("first _id timestamp:", first._id.getTimestamp());
  print("last _id timestamp:", last._id.getTimestamp());
  const span_sec = (last._id.getTimestamp() - first._id.getTimestamp()) / 1000;
  print("span seconds:", span_sec, " — rate:", (cnt / Math.max(span_sec, 1)).toFixed(3), "snapshots/sec");
}
'

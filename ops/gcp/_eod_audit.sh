#!/usr/bin/env bash
# End-of-day audit: signals + persistence on the live GCP VM
cd /opt/option_trading
echo "VM time IST: $(TZ='Asia/Kolkata' date '+%H:%M:%S')"
echo ""

echo "================================================="
echo "1. SNAPSHOT PERSISTENCE (today)"
echo "================================================="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
const today = "2026-05-26";
const cnt = c.countDocuments({trade_date_ist: today});
print("snapshots today:", cnt);
const first = c.find({trade_date_ist:today}).sort({_id:1}).limit(1).toArray()[0];
const last  = c.find({trade_date_ist:today}).sort({_id:-1}).limit(1).toArray()[0];
if (first) print("first:", first.market_time_ist || "(no market_time)");
if (last)  print("last :", last.market_time_ist  || "(no market_time)");
if (last) {
  const snap = (last.payload || {}).snapshot || {};
  print("strikes captured in last snapshot:", (snap.strikes || []).length);
  print("atm_ce_close:", (snap.atm_options || {}).atm_ce_close);
  print("atm_pe_close:", (snap.atm_options || {}).atm_pe_close);
  print("futures_close:", (snap.futures_bar || {}).fut_close);
  print("vix:", (snap.vix_context || {}).vix_current);
  print("regime:", (snap.session_context || {}).regime);
}
'

echo ""
echo "================================================="
echo "2. OPTION CHAIN PERSISTENCE — sample 3 minutes through the day"
echo "================================================="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").phase1_market_snapshots;
["10:30:00", "12:30:00", "15:25:00"].forEach(t => {
  const d = c.findOne({trade_date_ist:"2026-05-26", market_time_ist:t});
  if (d) {
    const snap = (d.payload || {}).snapshot || {};
    const strikes = (snap.strikes || []).length;
    const atm = (snap.atm_options || {});
    print(t, " | strikes:", strikes, " | CE@" + atm.atm_ce_strike + "=" + atm.atm_ce_close, " | PE@" + atm.atm_pe_strike + "=" + atm.atm_pe_close, " | fut=" + ((snap.futures_bar || {}).fut_close));
  } else {
    print(t, " | (no snapshot at this time)");
  }
});
'

echo ""
echo "================================================="
echo "3. DEPTH HISTORY — is it persisted anywhere?"
echo "================================================="
echo "--- Redis current depth (TTL-based, latest only) ---"
sudo docker compose exec -T redis redis-cli GET 'live:depth:atm_ce:latest'
sudo docker compose exec -T redis redis-cli GET 'live:depth:atm_pe:latest'
echo "--- Mongo: any depth collection? ---"
sudo docker compose exec -T mongo mongosh --quiet --eval '
const db_ = db.getSiblingDB("trading_ai");
db_.getCollectionNames().filter(n => n.match(/depth|tick|bid_ask|orderbook/i)).forEach(n => {
  print(n, ":", db_.getCollection(n).countDocuments({}));
});
print("(if empty: depth is Redis-only — TTL 60s, no history kept by design)");
'

echo ""
echo "================================================="
echo "4. STRATEGY OUTPUTS (today only)"
echo "================================================="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const db_ = db.getSiblingDB("trading_ai");
const today = "2026-05-26";
print("strategy_decision_traces today:", db_.strategy_decision_traces.countDocuments({trade_date_ist:today}));
print("strategy_votes today           :", db_.strategy_votes.countDocuments({trade_date_ist:today}));
print("trade_signals today            :", db_.trade_signals.countDocuments({trade_date_ist:today}));
print("strategy_positions today       :", db_.strategy_positions.countDocuments({trade_date_ist:today}));
'

echo ""
echo "================================================="
echo "5. WHY NO SIGNALS? — Inspect decision traces"
echo "================================================="
sudo docker compose exec -T mongo mongosh --quiet --eval '
const c = db.getSiblingDB("trading_ai").strategy_decision_traces;
const today = "2026-05-26";
const cnt = c.countDocuments({trade_date_ist:today});
print("decision traces today:", cnt);
if (cnt > 0) {
  // count by outcome / verdict
  const buckets = c.aggregate([
    {$match: {trade_date_ist: today}},
    {$group: {_id: {verdict:"$verdict", outcome:"$outcome", reason_top:"$reason"}, n: {$sum:1}}},
    {$sort: {n: -1}},
    {$limit: 15}
  ]).toArray();
  print("top reasons / verdicts:");
  buckets.forEach(b => print("  ", b.n, " : ", JSON.stringify(b._id)));
  print("");
  print("--- a sample decision ---");
  const d = c.find({trade_date_ist:today}).sort({_id:-1}).limit(1).toArray()[0];
  if (d) {
    print("keys:", Object.keys(d).slice(0,30).join(","));
    print("market_time_ist:", d.market_time_ist);
    print("regime:", d.regime || d.regime_context);
    print("verdict:", d.verdict);
    print("outcome:", d.outcome);
    print("reason:", d.reason);
  }
}
'

echo ""
echo "================================================="
echo "6. strategy_app recent activity (last 30 min)"
echo "================================================="
sudo docker compose logs --since=30m strategy_app 2>&1 | grep -viE "ignored non-json|health published" | tail -30

echo ""
echo "================================================="
echo "7. persistence health (final totals)"
echo "================================================="
sudo docker compose logs --since=2m persistence_app 2>&1 | grep -E "consumed|written" | tail -3
echo "---"
sudo docker compose logs --since=2m strategy_persistence_app 2>&1 | grep -E "consumed|written" | tail -3

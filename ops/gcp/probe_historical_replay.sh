#!/usr/bin/env bash
set -euo pipefail

START_DATE="${1:-2024-08-01}"
END_DATE="${2:-2024-09-01}"
LABEL="${3:-window}"
REPLAY_STATUS_JSON="$(sudo docker exec option_trading-redis-1 redis-cli GET system:historical:replay_status 2>/dev/null || true)"
REPLAY_STARTED_AT="$(
  printf '%s' "${REPLAY_STATUS_JSON}" \
    | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); print((json.loads(raw).get("started_at") if raw else "") or "")' \
    2>/dev/null || true
)"

echo "=== Probe ${LABEL} $(date -Is) ==="

echo
echo "=== Replay status ==="
printf '%s\n' "${REPLAY_STATUS_JSON}" \
  | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); print(raw if raw else "EMPTY")'

echo
echo "=== Redis topic state ==="
sudo docker exec option_trading-redis-1 redis-cli PUBSUB NUMSUB market:snapshot:v1:historical 2>/dev/null || true
sudo docker exec option_trading-redis-1 redis-cli LLEN market:snapshot:v1:historical 2>/dev/null || true
sudo docker exec option_trading-redis-1 redis-cli XLEN market:snapshot:v1:historical 2>/dev/null || true

echo
echo "=== Recent strategy logs ==="
sudo docker logs option_trading-strategy_app_historical-1 --since 3m 2>&1 \
  | grep -E 'session started|session ended|position opened|position closed|signal entry|signal exit|ERROR' \
  | tail -40 || true

echo
echo "=== Mongo counts ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {timestamp:{\$gte:'${START_DATE}',\$lt:'${END_DATE}'}};
var latest = db.strategy_positions_historical.find(q).sort({_id:-1}).limit(1).toArray();
var latestRunId = latest.length ? latest[0].run_id : null;
var replayStartedAt = '${REPLAY_STARTED_AT}';
var replayFilter = replayStartedAt ? {received_at_ttl:{\$gte:ISODate(replayStartedAt)}} : null;
printjson({
  trade_signals: db.trade_signals_historical.countDocuments(q),
  position_open_signals: db.trade_signals_historical.countDocuments({...q,event_type:'position_open'}),
  position_close_signals: db.trade_signals_historical.countDocuments({...q,event_type:'position_close'}),
  positions: db.strategy_positions_historical.countDocuments(q),
  votes: db.strategy_votes_historical.countDocuments(q),
  traces: db.strategy_decision_traces.countDocuments(q),
  phase1_snapshots: db.phase1_market_snapshots_historical.countDocuments(q),
  latest_position_run_id: latestRunId,
  replay_started_at: replayStartedAt || null,
  replay_scoped_trade_signals: replayFilter ? db.trade_signals_historical.countDocuments(replayFilter) : null,
  replay_scoped_positions: replayFilter ? db.strategy_positions_historical.countDocuments(replayFilter) : null,
  replay_scoped_votes: replayFilter ? db.strategy_votes_historical.countDocuments(replayFilter) : null,
  replay_scoped_traces: replayFilter ? db.strategy_decision_traces.countDocuments(replayFilter) : null,
  run_scoped_trade_signals: latestRunId ? db.trade_signals_historical.countDocuments({run_id:latestRunId}) : null,
  run_scoped_positions: latestRunId ? db.strategy_positions_historical.countDocuments({run_id:latestRunId}) : null,
  run_scoped_votes: latestRunId ? db.strategy_votes_historical.countDocuments({run_id:latestRunId}) : null,
  run_scoped_traces: latestRunId ? db.strategy_decision_traces.countDocuments({run_id:latestRunId}) : null
});
"

echo
echo "=== Latest positions ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {timestamp:{\$gte:'${START_DATE}',\$lt:'${END_DATE}'}};
db.strategy_positions_historical.find(q).sort({_id:-1}).limit(5).forEach(function(p){
  print(JSON.stringify({
    run_id:p.run_id,
    position_id:p.position_id,
    timestamp:p.timestamp,
    event_type:p.event_type,
    direction:p.direction,
    entry_futures_price:p.entry_futures_price,
    underlying_stop_pct:p.underlying_stop_pct,
    underlying_target_pct:p.underlying_target_pct,
    exit_reason:p.exit_reason,
    pnl_pct:p.pnl_pct
  }));
});
"

echo
echo "=== Underlying field completeness ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {timestamp:{\$gte:'${START_DATE}',\$lt:'${END_DATE}'}};
printjson({
  missing_underlying_stop: db.strategy_positions_historical.countDocuments({...q,underlying_stop_pct:null}),
  missing_underlying_target: db.strategy_positions_historical.countDocuments({...q,underlying_target_pct:null}),
  present_underlying_stop: db.strategy_positions_historical.countDocuments({...q,underlying_stop_pct:{\$ne:null}}),
  present_underlying_target: db.strategy_positions_historical.countDocuments({...q,underlying_target_pct:{\$ne:null}})
});
"

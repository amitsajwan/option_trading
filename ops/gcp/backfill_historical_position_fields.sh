#!/usr/bin/env bash
set -euo pipefail

DATE_FROM="${1:?usage: backfill_historical_position_fields.sh DATE_FROM DATE_TO}"
DATE_TO="${2:?usage: backfill_historical_position_fields.sh DATE_TO is exclusive upper bound}"

echo "=== Before backfill ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {trade_date_ist:{\$gte:'${DATE_FROM}',\$lt:'${DATE_TO}'}};
printjson({
  docs: db.strategy_positions_historical.countDocuments(q),
  top_level_underlying_stop_present: db.strategy_positions_historical.countDocuments({...q,underlying_stop_pct:{\$ne:null}}),
  top_level_underlying_target_present: db.strategy_positions_historical.countDocuments({...q,underlying_target_pct:{\$ne:null}}),
  payload_underlying_stop_present: db.strategy_positions_historical.countDocuments({...q,'payload.position.underlying_stop_pct':{\$ne:null}}),
  payload_underlying_target_present: db.strategy_positions_historical.countDocuments({...q,'payload.position.underlying_target_pct':{\$ne:null}}),
  payload_entry_futures_present: db.strategy_positions_historical.countDocuments({...q,'payload.position.entry_futures_price':{\$ne:null}})
});
"

echo
echo "=== Applying backfill from payload.position ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {trade_date_ist:{\$gte:'${DATE_FROM}',\$lt:'${DATE_TO}'}};
var result = db.strategy_positions_historical.updateMany(
  q,
  [
    {
      \$set: {
        entry_premium: {\$ifNull:['\$entry_premium','\$payload.position.entry_premium']},
        current_premium: {\$ifNull:['\$current_premium','\$payload.position.current_premium']},
        exit_premium: {\$ifNull:['\$exit_premium','\$payload.position.exit_premium']},
        pnl_pct: {\$ifNull:['\$pnl_pct','\$payload.position.pnl_pct']},
        mfe_pct: {\$ifNull:['\$mfe_pct','\$payload.position.mfe_pct']},
        mae_pct: {\$ifNull:['\$mae_pct','\$payload.position.mae_pct']},
        bars_held: {\$ifNull:['\$bars_held','\$payload.position.bars_held']},
        lots: {\$ifNull:['\$lots','\$payload.position.lots']},
        stop_loss_pct: {\$ifNull:['\$stop_loss_pct','\$payload.position.stop_loss_pct']},
        stop_price: {\$ifNull:['\$stop_price','\$payload.position.stop_price']},
        high_water_premium: {\$ifNull:['\$high_water_premium','\$payload.position.high_water_premium']},
        target_pct: {\$ifNull:['\$target_pct','\$payload.position.target_pct']},
        entry_futures_price: {\$ifNull:['\$entry_futures_price','\$payload.position.entry_futures_price']},
        underlying_stop_pct: {\$ifNull:['\$underlying_stop_pct','\$payload.position.underlying_stop_pct']},
        underlying_target_pct: {\$ifNull:['\$underlying_target_pct','\$payload.position.underlying_target_pct']},
        exit_reason: {\$ifNull:['\$exit_reason','\$payload.position.exit_reason']}
      }
    }
  ]
);
printjson(result);
"

echo
echo "=== After backfill ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {trade_date_ist:{\$gte:'${DATE_FROM}',\$lt:'${DATE_TO}'}};
printjson({
  docs: db.strategy_positions_historical.countDocuments(q),
  top_level_underlying_stop_present: db.strategy_positions_historical.countDocuments({...q,underlying_stop_pct:{\$ne:null}}),
  top_level_underlying_target_present: db.strategy_positions_historical.countDocuments({...q,underlying_target_pct:{\$ne:null}}),
  top_level_entry_futures_present: db.strategy_positions_historical.countDocuments({...q,entry_futures_price:{\$ne:null}})
});
db.strategy_positions_historical.find(q).sort({_id:-1}).limit(3).forEach(function(p){
  print(JSON.stringify({
    position_id:p.position_id,
    timestamp:p.timestamp,
    event:p.event,
    entry_futures_price:p.entry_futures_price,
    underlying_stop_pct:p.underlying_stop_pct,
    underlying_target_pct:p.underlying_target_pct,
    exit_reason:p.exit_reason,
    pnl_pct:p.pnl_pct
  }));
});
"

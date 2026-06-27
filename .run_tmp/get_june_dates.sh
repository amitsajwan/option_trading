#!/bin/bash
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval 'var d=db.phase1_market_snapshots.distinct("trade_date_ist",{trade_date_ist:{$gte:"2026-06-01",$lte:"2026-06-30"}}); d.sort(); print("JUNE_DATES="+d.join(",")); for(var i=0;i<d.length;i++){var cnt=db.phase1_market_snapshots.countDocuments({trade_date_ist:d[i]}); print("  "+d[i]+": "+cnt+" snapshots"); }'

#!/usr/bin/env bash
# Check MongoDB collections for historical snapshot data
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval '
var colls = ["phase1_market_snapshots", "phase1_market_snapshots_historical"];
colls.forEach(function(c) {
  var cnt = db[c].countDocuments({});
  print(c + ": " + cnt);
  if (cnt > 0) {
    var oldest = db[c].find({},{trade_date:1,_id:0}).sort({trade_date:1}).limit(1).toArray();
    var newest = db[c].find({},{trade_date:1,_id:0}).sort({trade_date:-1}).limit(1).toArray();
    print("  oldest: " + JSON.stringify(oldest[0]));
    print("  newest: " + JSON.stringify(newest[0]));
  }
});
'

// Find the newest run_id by looking at recent POSITION_OPEN docs
var recent = db.strategy_positions_historical
  .find({event: "POSITION_OPEN"}, {run_id:1, trade_date_ist:1, _id:0})
  .sort({_id: -1})
  .limit(5)
  .toArray();
var seen = {};
recent.forEach(function(d) {
  if (!seen[d.run_id]) {
    seen[d.run_id] = d.trade_date_ist;
    print(d.run_id, d.trade_date_ist);
  }
});

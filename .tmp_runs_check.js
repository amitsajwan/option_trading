// Check runs registry and whether trades are tagged with run_id
print('=== strategy_eval_runs (last 10) ===');
db.strategy_eval_runs.find({}, {run_id:1,dataset:1,status:1,date_from:1,date_to:1,submitted_at:1,ended_at:1,progress_pct:1,message:1,_id:0})
  .sort({submitted_at:-1}).limit(10).forEach(function(d){ print(JSON.stringify(d)); });

print('');
print('=== strategy_positions_historical: run_id distribution ===');
var posAgg = db.strategy_positions_historical.aggregate([
  { $group: { _id: '$run_id', count: { $sum: 1 }, min_date: { $min: '$trade_date_ist' }, max_date: { $max: '$trade_date_ist' } } },
  { $sort: { count: -1 } },
  { $limit: 10 },
]).toArray();
posAgg.forEach(function(d){ print(JSON.stringify(d)); });

print('');
print('=== trade_signals_historical: run_id distribution ===');
var sigAgg = db.trade_signals_historical.aggregate([
  { $group: { _id: '$run_id', count: { $sum: 1 } } },
  { $sort: { count: -1 } },
  { $limit: 10 },
]).toArray();
sigAgg.forEach(function(d){ print(JSON.stringify(d)); });

print('');
print('=== total counts ===');
print('positions_historical.total=' + db.strategy_positions_historical.countDocuments({}));
print('positions_historical.with_run_id=' + db.strategy_positions_historical.countDocuments({ run_id: { $ne: null } }));
print('signals_historical.total=' + db.trade_signals_historical.countDocuments({}));
print('signals_historical.with_run_id=' + db.trade_signals_historical.countDocuments({ run_id: { $ne: null } }));

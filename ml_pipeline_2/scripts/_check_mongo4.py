import subprocess

out = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "trading_ai", "--quiet", "--eval",
    '''
var d = db.strategy_positions_historical.findOne();
print(JSON.stringify({
  run_id: d && d.run_id,
  direction: d && d.direction,
  event_type: d && d.event_type,
  reason: d && (d.reason||"").slice(0,120),
  keys: d && Object.keys(d).join(",")
}));
'''
])
print("pos sample:", out.decode().strip())

# Check what run_ids exist
out2 = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "trading_ai", "--quiet", "--eval",
    'db.strategy_positions_historical.distinct("run_id").forEach(r => print(r))'
])
print("run_ids:", out2.decode().strip())

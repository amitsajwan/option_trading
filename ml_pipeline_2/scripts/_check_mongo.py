import subprocess, json

out = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'var d = db.strategy_positions_historical.findOne(); print(JSON.stringify({run_id: d && d.run_id, direction: d && d.direction, reason: d && d.reason && d.reason.slice(0,120)}))'
])
print("latest pos:", out.decode().strip())

out2 = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'print(db.strategy_positions_historical.countDocuments())'
])
print("total positions:", out2.decode().strip())

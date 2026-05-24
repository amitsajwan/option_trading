import subprocess, json

# Check positions collection for direction field
out = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'JSON.stringify(db.positions.findOne({},{direction:1,dir:1,entry_strategy:1,action:1,_id:0,entry_reason:1}))'
])
print("positions sample:", out.decode().strip())

# Check what keys exist
out2 = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'JSON.stringify(Object.keys(db.positions.findOne({})||{}))'
])
print("positions keys:", out2.decode().strip())

# Check signals
out3 = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'JSON.stringify(Object.keys(db.signals.findOne({})||{}))'
])
print("signals keys:", out3.decode().strip())

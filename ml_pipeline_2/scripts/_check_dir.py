import subprocess, json
out = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'JSON.stringify(db.signals.findOne({direction:{$exists:true}},{direction:1,entry_strategy:1,action:1,_id:0}))'
])
print(out.decode())

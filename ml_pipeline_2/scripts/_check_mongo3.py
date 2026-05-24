import subprocess

out = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "trading_ai", "--quiet", "--eval",
    'db.getCollectionNames().forEach(c => print(c, db[c].countDocuments()))'
])
print("trading_ai collections:", out.decode().strip())

# Sample a position doc to see run_id and direction
out2 = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "trading_ai", "--quiet", "--eval",
    '''
var colls = db.getCollectionNames().filter(c => c.includes("position"));
colls.forEach(c => {
  var d = db[c].findOne({event_type: "POSITION_OPEN"});
  if(d) print(c, JSON.stringify({run_id: d.run_id, direction: d.direction, reason: (d.reason||"").slice(0,100)}));
});
'''
])
print("position samples:", out2.decode().strip())

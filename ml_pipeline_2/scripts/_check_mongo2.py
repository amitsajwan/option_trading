import subprocess

out = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "--quiet", "--eval",
    'db.adminCommand({listDatabases:1}).databases.forEach(d => print(d.name))'
])
print("databases:", out.decode().strip())

out2 = subprocess.check_output([
    "docker", "exec", "option_trading-mongo-1",
    "mongosh", "option_trading", "--quiet", "--eval",
    'db.getCollectionNames().forEach(c => print(c, db[c].countDocuments()))'
])
print("collections:", out2.decode().strip())

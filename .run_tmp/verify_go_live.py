import subprocess, sys, json, os

# Set Dhan token
ENV_FILE = "/opt/option_trading/.env.compose"

def check_line(content, label):
    print(f"\n=== {label} ===")
    print(content.strip())
    return content.strip()

# 0.1: execution_app status
r = subprocess.run("sudo docker ps --filter name=execution --format '{{.Names}} {{.Status}}'", shell=True, capture_output=True, text=True)
check_line(r.stdout or "NOT_RUNNING", "0.1 execution_app")

# 0.3: Dhan token
with open(ENV_FILE) as f:
    content = f.read()
dhan_token = [l for l in content.split('\n') if l.startswith('DHAN_ACCESS_TOKEN=')]
check_line(dhan_token[0] if dhan_token else "NOT_SET", "0.3 Dhan token")

# 0.6: EXECUTION_ADAPTER
adapter = [l for l in content.split('\n') if 'EXECUTION_ADAPTER' in l]
check_line(adapter[0] if adapter else "NOT_SET", "0.6 EXECUTION_ADAPTER")

# 0.7: RISK_MAX_LOTS_PER_TRADE
lots = [l for l in content.split('\n') if l.startswith('RISK_MAX_LOTS_PER_TRADE=')]
check_line(lots[0] if lots else "NOT_SET", "0.7 RISK_MAX_LOTS_PER_TRADE")

# 1.2: Live config key checks
keys = ['ENTRY_ML_MODEL_PATH', 'ENTRY_ML_MIN_PROB', 'SIDEWAYS_RETURNS_MIXED_GATE_ENABLED', 'RISK_MAX_SESSION_TRADES']
for k in keys:
    val = [l for l in content.split('\n') if l.startswith(f'{k}=')]
    if val:
        print(f"  {val[0]}")
    else:
        print(f"  {k}= NOT SET")

# 2.1: Latest snapshot
print("\n=== 2.1 Latest snapshot ===")
r = subprocess.run("sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval 'var d=db.phase1_market_snapshots.find().sort({market_time_ist:-1}).limit(1).next(); print(\"latest=\"+d.market_time_ist);'", shell=True, capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip())

# 2.3: Ingestion status
r = subprocess.run("sudo docker ps --filter name=ingestion --format '{{.Names}} {{.Status}}'", shell=True, capture_output=True, text=True)
check_line(r.stdout or "NOT_RUNNING", "2.3 ingestion_app")

# 4.1: Redis
r = subprocess.run("sudo docker exec option_trading-redis-1 redis-cli ping", shell=True, capture_output=True, text=True)
check_line(r.stdout or r.stderr, "4.1 Redis")

# 4.2: Mongo
r = subprocess.run("sudo docker ps --filter name=mongo --format '{{.Names}} {{.Status}}'", shell=True, capture_output=True, text=True)
check_line(r.stdout or "NOT_RUNNING", "4.2 Mongo")

print("\n" + "=" * 50)
print("GO/NO-GO SUMMARY:")
print("=" * 50)

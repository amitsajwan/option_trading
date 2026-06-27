import json, urllib.request, subprocess

# Get latest snapshot from MongoDB
js = 'var d = db.phase1_market_snapshots.find({trade_date_ist: "2026-06-18"}).sort({market_time_ist:-1}).limit(1).next(); print(JSON.stringify(d.payload));'
with open('/tmp/snap_payload.js', 'w') as f:
    f.write(js)

subprocess.run("sudo docker cp /tmp/snap_payload.js option_trading-mongo-1:/tmp/snap_payload.js", shell=True)
r = subprocess.run("sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet /tmp/snap_payload.js", shell=True, capture_output=True, text=True)

payload_str = r.stdout.strip()
if not payload_str:
    print("ERROR: no snapshot found")
    exit(1)

payload = json.loads(payload_str)

# Publish to Redis
import redis
client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
count = client.publish('market:snapshot:v1', json.dumps(payload, default=str))
print(f"Published snapshot {payload.get('snapshot_id')} to market:snapshot:v1, reached {count} subscribers")

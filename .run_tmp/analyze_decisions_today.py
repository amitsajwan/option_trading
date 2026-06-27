import subprocess, json
from collections import Counter

date = "2026-06-18"

# Pull decision_traces from mongo
js = f"""
var docs = db.decision_traces.find({{trade_date_ist: "{date}"}}).toArray();
print(JSON.stringify(docs));
"""

with open('/tmp/dec_query.js', 'w') as f:
    f.write(js)

subprocess.run("sudo docker cp /tmp/dec_query.js option_trading-mongo-1:/tmp/dec_query.js", shell=True)
r = subprocess.run("sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet /tmp/dec_query.js",
                   shell=True, capture_output=True, text=True)
raw = r.stdout.strip()
if not raw or raw == '[]':
    # try direct collection name lookup
    js2 = """
var cols = db.getCollectionNames();
print(JSON.stringify(cols));
"""
    with open('/tmp/cols_query.js', 'w') as f:
        f.write(js2)
    subprocess.run("sudo docker cp /tmp/cols_query.js option_trading-mongo-1:/tmp/cols_query.js", shell=True)
    r2 = subprocess.run("sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet /tmp/cols_query.js",
                        shell=True, capture_output=True, text=True)
    print("Collections:", r2.stdout.strip()[:500])
    print("No decision_traces found for today")
else:
    try:
        docs = json.loads(raw)
        print(f"Found {len(docs)} decision traces")
        blocked = Counter()
        signals = []
        for d in docs:
            action = d.get('action') or d.get('type') or '?'
            if action in ('blocked', 'block'):
                blocked[d.get('blocking_gate') or d.get('blocker') or 'unknown'] += 1
            elif action in ('signal', 'entry'):
                signals.append(d)
        print(f"Signals: {len(signals)}, Blocked: {sum(blocked.values())}")
        print("Blocker distribution:")
        for b, c in blocked.most_common():
            print(f"  {b}: {c}")
        print("\nSignals:")
        for s in signals:
            t = (s.get('timestamp') or '')[:16]
            print(f"  {t} {s.get('direction')} {s.get('strike')} conf={s.get('confidence')}")
    except Exception as e:
        print(f"Parse error: {e}")
        print("Raw:", raw[:500])

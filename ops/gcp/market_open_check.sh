#!/usr/bin/env bash
# 09:20 IST — "market up" verdict: are live snapshots actually flowing for both
# instruments? (2026-07-07 the stack ran token-less until 10:52 with zero
# snapshots and nothing said so.)
REPO=/opt/option_trading
TG="bash $REPO/ops/gcp/tg_send.sh"
log(){ echo "[$(date -u +%FT%TZ)] $*"; }

check_one(){ # container -> "OK ts" | "STALE ts" | "NONE"
  docker exec "$1" python3 - <<'EOF' 2>/dev/null
import json, pathlib, datetime
paths = ["/app/.run/snapshot_app/events.jsonl", "/app/.run/snapshot_app_nifty/events.jsonl"]
p = next((pathlib.Path(x) for x in paths if pathlib.Path(x).exists()), None)
if p is None:
    print("NONE"); raise SystemExit
lines = p.read_text().splitlines()
ts = ""
for line in reversed(lines[-5:]):
    try:
        s = json.loads(line).get("snapshot", {})
        ts = str(s.get("timestamp", ""))
        break
    except Exception:
        continue
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
try:
    t = datetime.datetime.fromisoformat(ts)
    age = (now - t).total_seconds()
    print(("OK " if age < 240 else "STALE ") + ts[11:16])
except Exception:
    print("NONE")
EOF
}

bn=$(check_one option_trading-snapshot_app-1)
nf=$(check_one option_trading-snapshot_app_nifty-1)

if [[ "$bn" == OK* && "$nf" == OK* ]]; then
  msg="📈 MARKET UP $(TZ=Asia/Kolkata date +%H:%M) IST — snapshots flowing (BN ${bn#OK }, NIFTY ${nf#OK })"
else
  msg="🚨 SNAPSHOTS NOT FLOWING $(TZ=Asia/Kolkata date +%H:%M) IST — BN: $bn | NIFTY: $nf — check ingestion/token NOW"
fi
log "$msg"
$TG "$msg"

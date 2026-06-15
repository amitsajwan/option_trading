#!/bin/bash
# Headless daily Kite token refresh (runs as root via cron, pre-open).
# Fixes the daily-expiry blocker: refreshes BOTH credentials.json (ingestion/
# strategy/snapshot) AND KITE_ACCESS_TOKEN in .env.compose (depth collector),
# then restarts the consumers. Secrets live root-only in /opt/option_trading/.kite_secrets.
set -uo pipefail
cd /opt/option_trading
log() { echo "$(date '+%F %T') kite_refresh: $*"; }

[ -f .kite_secrets ] || { log "ERROR no .kite_secrets"; exit 1; }
. ./.kite_secrets   # KITE_PASSWORD, KITE_TOTP_SECRET

AK=$(python3 -c "import json;print(json.load(open('ingestion_app/credentials.json'))['api_key'])")
AS=$(python3 -c "import json;print(json.load(open('ingestion_app/credentials.json'))['api_secret'])")
UID_=$(python3 -c "import json;print(json.load(open('ingestion_app/credentials.json')).get('user_id','BV2032'))")

log "running headless TOTP login for $UID_"
docker exec -e KITE_API_KEY="$AK" -e KITE_API_SECRET="$AS" -e KITE_USER_ID="$UID_" \
  -e KITE_PASSWORD="$KITE_PASSWORD" -e KITE_TOTP_SECRET="$KITE_TOTP_SECRET" \
  -e KITE_CREDENTIALS_PATH=/tmp/newcreds.json -e KITE_SKIP_ENV_UPDATE=1 \
  option_trading-ingestion_app-1 python -m ingestion_app.kite_totp_auth || { log "ERROR auth failed"; exit 2; }

docker cp option_trading-ingestion_app-1:/tmp/newcreds.json /tmp/newcreds.json || exit 3
python3 -c "import json;d=json.load(open('/tmp/newcreds.json'));assert len(d.get('access_token',''))>=20" || { log "ERROR bad creds"; exit 4; }

# install: credentials.json (in place to keep the bind mount), and .env.compose token
cp /tmp/newcreds.json ingestion_app/credentials.json
AT=$(python3 -c "import json;print(json.load(open('/tmp/newcreds.json'))['access_token'])")
python3 - "$AT" <<'PY'
import sys
at=sys.argv[1]; p='/opt/option_trading/.env.compose'
lines=open(p).read().splitlines(); done=False
for i,l in enumerate(lines):
    if l.startswith('KITE_ACCESS_TOKEN='): lines[i]='KITE_ACCESS_TOKEN='+at; done=True
if not done: lines.append('KITE_ACCESS_TOKEN='+at)
open(p,'w').write('\n'.join(lines)+'\n')
PY

log "restarting ingestion + snapshot + depth"
docker restart option_trading-ingestion_app-1 option_trading-snapshot_app-1 option_trading-depth_collector-1 >/dev/null 2>&1
log "DONE"

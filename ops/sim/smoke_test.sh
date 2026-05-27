#!/usr/bin/env bash
set -euo pipefail

# SIM replay smoke test (SIM-9)
# - starts a sim run through dashboard API
# - polls until terminal status
# - verifies manifest + sealed run dir + sim collection writes
# - attempts cleanup via DELETE

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8008}"
MONGO_DB="${MONGO_DB:-trading_ai}"
SOURCE_COLL="${SOURCE_COLL:-phase1_market_snapshots}"
LABEL="${LABEL:-smoke_test}"
SPEED="${SPEED:-30}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-2}"
POLL_TIMEOUT_SEC="${POLL_TIMEOUT_SEC:-240}"

log() { printf '[sim-smoke] %s\n' "$*"; }
fail() { printf '[sim-smoke][ERROR] %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

need_cmd curl
need_cmd python

log "resolving source_date from Mongo (>=100 snapshots in ${SOURCE_COLL})"
SOURCE_DATE="${SOURCE_DATE:-$(
python - <<'PY'
import os
from pymongo import MongoClient

uri = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
if uri:
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
else:
    client = MongoClient(
        host=os.getenv("MONGO_HOST", "localhost"),
        port=int(os.getenv("MONGO_PORT", "27017")),
        serverSelectionTimeoutMS=3000,
    )
db = client[os.getenv("MONGO_DB", "trading_ai")]
coll = db[os.getenv("SOURCE_COLL", "phase1_market_snapshots")]
pipeline = [
    {"$group": {"_id": "$trade_date_ist", "n": {"$sum": 1}}},
    {"$match": {"_id": {"$type": "string"}, "n": {"$gte": 100}}},
    {"$sort": {"_id": -1}},
    {"$limit": 1},
]
row = next(coll.aggregate(pipeline), None)
if not row:
    raise SystemExit("no source date found with >=100 snapshots")
print(str(row["_id"]))
PY
)}"

log "using source_date=${SOURCE_DATE}"

POST_BODY="$(python - <<PY
import json, os
print(json.dumps({
  "source_date": os.getenv("SOURCE_DATE"),
  "source_coll": os.getenv("SOURCE_COLL", "phase1_market_snapshots"),
  "label": os.getenv("LABEL", "smoke_test"),
  "speed": float(os.getenv("SPEED", "30")),
  "env_overrides": {}
}))
PY
)"

log "POST /api/sim/runs"
CREATE_JSON="$(
  curl -fsS -X POST \
    -H "content-type: application/json" \
    "${API_BASE_URL}/api/sim/runs" \
    -d "${POST_BODY}"
)"

RUN_ID="$(python - <<PY
import json
payload = json.loads('''${CREATE_JSON}''')
rid = str(payload.get("run_id") or "").strip()
if not rid:
    raise SystemExit("POST /api/sim/runs missing run_id")
print(rid)
PY
)"

log "run_id=${RUN_ID}"
START_TS="$(date +%s)"
TERMINAL_STATUS=""

while true; do
  NOW_TS="$(date +%s)"
  ELAPSED="$((NOW_TS - START_TS))"
  if [ "${ELAPSED}" -gt "${POLL_TIMEOUT_SEC}" ]; then
    fail "timeout waiting for completion (run_id=${RUN_ID})"
  fi

  RUN_JSON="$(curl -fsS "${API_BASE_URL}/api/sim/runs/${RUN_ID}")"
  TERMINAL_STATUS="$(python - <<PY
import json
payload = json.loads('''${RUN_JSON}''')
print(str(payload.get("terminal_status") or payload.get("status") or ""))
PY
)"
  log "status=${TERMINAL_STATUS} elapsed=${ELAPSED}s"
  if [ "${TERMINAL_STATUS}" = "completed" ] || [ "${TERMINAL_STATUS}" = "cancelled" ] || [ "${TERMINAL_STATUS}" = "failed" ]; then
    break
  fi
  sleep "${POLL_INTERVAL_SEC}"
done

[ "${TERMINAL_STATUS}" = "completed" ] || fail "run did not complete cleanly (status=${TERMINAL_STATUS})"

log "verifying manifest + sealed run dir + collection writes"
python - <<'PY'
import os
import stat
from pathlib import Path

from pymongo import MongoClient
from contracts_app import resolve_namespace

run_id = os.environ["RUN_ID"]
ns = resolve_namespace("sim", run_id=run_id)
run_dir = ns.run_dir_for()
manifest = run_dir / "manifest.json"
if not manifest.exists():
    raise SystemExit(f"manifest missing: {manifest}")

# Sealed means no write bits for owner/group/other.
for path in [run_dir, *run_dir.rglob("*")]:
    mode = path.stat().st_mode
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit(f"path still writable: {path}")

uri = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
if uri:
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
else:
    client = MongoClient(
        host=os.getenv("MONGO_HOST", "localhost"),
        port=int(os.getenv("MONGO_PORT", "27017")),
        serverSelectionTimeoutMS=3000,
    )
db = client[os.getenv("MONGO_DB", "trading_ai")]
for base in ("snapshots", "votes", "signals", "positions", "decision_traces"):
    coll = ns.collection_for(base)
    n = int(db[coll].count_documents({"run_id": run_id}))
    if n < 1:
        raise SystemExit(f"collection has no docs for run_id={run_id}: {coll}")
print("verification-ok")
PY

log "cleanup via DELETE /api/sim/runs/${RUN_ID}"
curl -fsS -X DELETE "${API_BASE_URL}/api/sim/runs/${RUN_ID}" >/dev/null || log "cleanup delete returned non-zero"

log "SMOKE TEST PASS (run_id=${RUN_ID})"

#!/usr/bin/env bash
set -euo pipefail

# SIM GC cron (SIM-10)
# - removes stale per-run directories older than N days
# - audits sim TTL posture in Mongo

RETENTION_DAYS="${SIM_GC_RETENTION_DAYS:-30}"
RUN_DIR_ROOT="${SIM_RUN_DIR_ROOT:-/opt/option_trading/.run/strategy_app_sim}"
LOG_PREFIX="[sim-gc]"

log() { printf '%s %s\n' "${LOG_PREFIX}" "$*"; }

if ! [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]]; then
  echo "${LOG_PREFIX} invalid SIM_GC_RETENTION_DAYS=${RETENTION_DAYS}" >&2
  exit 2
fi

log "start retention_days=${RETENTION_DAYS} run_dir_root=${RUN_DIR_ROOT}"

deleted_dirs=0
freed_bytes=0

if [[ ! -d "${RUN_DIR_ROOT}" ]]; then
  log "run dir root missing; no-op"
else
  while IFS= read -r dir; do
    [[ -n "${dir}" ]] || continue
    size="$(du -sb "${dir}" | awk '{print $1}')"
    rm -rf "${dir}"
    deleted_dirs=$((deleted_dirs + 1))
    freed_bytes=$((freed_bytes + size))
    log "deleted dir=${dir} bytes=${size}"
  done < <(find "${RUN_DIR_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime +"${RETENTION_DAYS}" 2>/dev/null || true)
fi

log "ttl-audit start"
python - <<'PY'
import os
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient
from contracts_app import resolve_namespace

days = int(os.getenv("SIM_GC_RETENTION_DAYS", "30"))
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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
ns = resolve_namespace("sim")

for base in ("snapshots", "votes", "signals", "positions", "decision_traces", "depth_ticks"):
    coll = ns.collection_for(base)
    try:
        stale = int(db[coll].count_documents({"created_at": {"$lt": cutoff}}))
    except Exception:
        stale = -1
    print(f"[sim-gc] ttl-audit collection={coll} stale_docs={stale} cutoff_utc={cutoff.isoformat()}")
PY

log "done deleted_dirs=${deleted_dirs} freed_bytes=${freed_bytes}"

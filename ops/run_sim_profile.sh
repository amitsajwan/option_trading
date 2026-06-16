#!/bin/bash
# Run a config PROFILE through the OPS SIM (same profile file used for live).
# Usage:  bash ops/run_sim_profile.sh <profile> <YYYY-MM-DD>
# Example: bash ops/run_sim_profile.sh shadow 2026-06-11
set -euo pipefail
PROFILE="${1:?profile name, e.g. shadow}"; DATE="${2:?date YYYY-MM-DD}"
F="ops/profiles/${PROFILE}.env"; [ -f "$F" ] || { echo "no profile $F"; exit 1; }
REQ=$(python3 -c "
import json,sys
ov={}
for l in open('$F'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); ov[k.strip()]=v.strip()
print(json.dumps({'date':'$DATE','overrides':ov}))")
echo "profile=$PROFILE date=$DATE overrides=$(echo "$REQ" | python3 -c 'import sys,json;print(list(json.load(sys.stdin)["overrides"]))')"
JOB=$(curl -s -X POST http://localhost:8008/api/ops/sim/today -H 'Content-Type: application/json' -d "$REQ")
JID=$(echo "$JOB" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("job_id") or "ERR:"+str(d.get("detail")))')
echo "job=$JID"; case "$JID" in ERR*) exit 1;; esac
for i in $(seq 1 30); do sleep 3; S=$(curl -s http://localhost:8008/api/ops/sim/$JID|python3 -c 'import sys,json;print(json.load(sys.stdin).get("status"))' 2>/dev/null); [ "$S" = done ] && break; done
sudo docker exec option_trading-dashboard-1 sh -c "cat /tmp/sim_$JID/session_summary.jsonl 2>/dev/null | head -1"

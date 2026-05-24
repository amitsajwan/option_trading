#!/usr/bin/env bash
# Full-window replay + per-trade forensics report (oos_primary default).
#
#   sudo bash ops/gcp/run_full_forensics_report.sh
#   sudo bash ops/gcp/run_full_forensics_report.sh oos_primary v1_direction_ml
#
# Output: /opt/option_trading/.run/forensics_reports/REPORT_<run_id>.txt
#         /opt/option_trading/.run/forensics_reports/trades_<run_id>.csv
#
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${REPO}/.env.compose"
WINDOW="${1:-oos_primary}"
PROFILE="${2:-v1_direction_ml}"
DATE_FROM="${DATE_FROM:-2024-05-01}"
DATE_TO="${DATE_TO:-2024-07-31}"
REFERENCE_RUN="${REFERENCE_RUN:-ae5a86b7-9198-4e64-9399-fd5fea03e293}"
REPORT_DIR="${REPO}/.run/forensics_reports"
mkdir -p "${REPORT_DIR}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

case "${WINDOW}" in
  oos_primary) DATE_FROM="2024-05-01"; DATE_TO="2024-07-31" ;;
  oos_secondary) DATE_FROM="2023-05-01"; DATE_TO="2023-07-31" ;;
  in_sample_sanity) DATE_FROM="2024-08-01"; DATE_TO="2024-10-31" ;;
esac

case "${PROFILE}" in
  v1_direction_ml) PATCH="${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh" ;;
  v1_dual) PATCH="${REPO}/ops/gcp/patch_trader_master_ml_entry_v1_dual_dir_env.sh" ;;
  *) echo "Unknown profile: ${PROFILE}" >&2; exit 2 ;;
esac

log() { echo "[$(date -Is)] $*" | tee -a "${REPORT_DIR}/latest_run.log"; }

write_section() {
  local title="$1"
  local body_file="$2"
  {
    echo ""
    echo "########################################################################"
    echo "# ${title}"
    echo "########################################################################"
    cat "${body_file}"
  } >> "${REPORT_FILE}"
}

cd "${REPO}"
git pull --ff-only origin main 2>/dev/null || true

REPORT_FILE="${REPORT_DIR}/REPORT_building.txt"
: > "${REPORT_FILE}"
log "=== Full forensics report: ${WINDOW} (${DATE_FROM} -> ${DATE_TO}) profile=${PROFILE} ==="

TMP="$(mktemp)"
{
  echo "Generated: $(date -Is)"
  echo "Window: ${DATE_FROM} -> ${DATE_TO}"
  echo "Profile: ${PROFILE}"
  echo ""
  echo "--- Parquet coverage (oos_primary) ---"
  "${PY}" "${REPO}/ops/gcp/check_parquet_coverage.py" 2>&1 || true
} > "${TMP}"
write_section "Data coverage" "${TMP}"

log "clean state + patch + rebuild historical"
sudo bash "${REPO}/ops/gcp/clean_state_before_replay.sh" >> "${REPORT_DIR}/latest_run.log" 2>&1
sudo bash "${PATCH}" "${ENV_FILE}" >> "${REPORT_DIR}/latest_run.log" 2>&1
sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${ENV_FILE}" >> "${REPORT_DIR}/latest_run.log" 2>&1

"${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
  build strategy_app_historical >> "${REPORT_DIR}/build.log" 2>&1
sudo docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --force-recreate --pull never strategy_app_historical >> "${REPORT_DIR}/latest_run.log" 2>&1
sleep 20
"${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 240 || true
"${PY}" "${REPO}/ops/gcp/preflight_historical_replay.py" >> "${REPORT_DIR}/latest_run.log" 2>&1 || true

log "queue replay ${DATE_FROM} -> ${DATE_TO}"
RID="$("${PY}" "${REPO}/ops/gcp/queue_replay.py" "${DATE_FROM}" "${DATE_TO}" | "${PY}" -c "
import json,sys
d=json.loads(sys.stdin.read())
print(d.get('run_id',''))
")"
if [ -z "${RID}" ]; then
  log "ERROR: no run_id from queue_replay"
  exit 1
fi
log "run_id=${RID}"

log "waiting for replay completion (up to 3h)"
for i in $(seq 1 360); do
  STATUS="$("${PY}" -c "
import json,urllib.request
rid='${RID}'
with urllib.request.urlopen(f'http://127.0.0.1:8008/api/strategy/evaluation/runs/{rid}',timeout=20) as r:
    print(json.load(r).get('status','').lower())
")"
  if [ "${i}" -eq 1 ] || [ $((i % 10)) -eq 0 ]; then
    log "  poll ${i}: status=${STATUS}"
  fi
  case "${STATUS}" in
    completed|failed|cancelled) break ;;
  esac
  sleep 30
done

mv "${REPORT_FILE}" "${REPORT_DIR}/REPORT_${RID}.building.txt"
REPORT_FILE="${REPORT_DIR}/REPORT_${RID}.txt"
mv "${REPORT_DIR}/REPORT_${RID}.building.txt" "${REPORT_FILE}"

sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_run.py" option_trading-dashboard-1:/tmp/analyze_oos_validation_run.py
sudo docker cp "${REPO}/ops/gcp/analyze_trade_forensics.py" option_trading-dashboard-1:/tmp/analyze_trade_forensics.py
sudo docker cp "${REPO}/ops/gcp/diagnose_oos_replay_coverage.py" option_trading-dashboard-1:/tmp/diagnose_oos_replay_coverage.py

CSV_PATH="/tmp/trades_${RID}.csv"

{
  echo "run_id: ${RID}"
  echo "status: ${STATUS}"
  echo ""
  sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/diagnose_oos_replay_coverage.py "${RID}"
} > "${TMP}"
write_section "Replay coverage (${RID})" "${TMP}"

{
  sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/analyze_oos_validation_run.py "${RID}" "${WINDOW}"
} > "${TMP}"
write_section "OOS summary (${RID})" "${TMP}"

{
  sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/analyze_trade_forensics.py --run-id "${RID}" \
    --date-from "${DATE_FROM}" --date-to "${DATE_TO}" \
    --top 15 --csv "${CSV_PATH}"
} > "${TMP}" 2>&1
write_section "Per-trade forensics (${RID})" "${TMP}"

sudo docker cp "option_trading-dashboard-1:${CSV_PATH}" "${REPORT_DIR}/trades_${RID}.csv" 2>/dev/null || true

# Reference run appendix if new run is thin (<80 closes or single month)
MONTHS="$("${PY}" -c "
from pymongo import MongoClient
db=MongoClient('mongodb://mongo:27017', serverSelectionTimeoutMS=8000)['trading_ai']
closes=list(db.strategy_positions_historical.find({'run_id':'${RID}','event':'POSITION_CLOSE'},{'trade_date_ist':1}))
months=set(str(d.get('trade_date_ist') or '')[:7] for d in closes)
print(len(closes), len(months))
" 2>/dev/null || echo "0 0")"
NCLOSES="$(echo "${MONTHS}" | awk '{print $1}')"
NMONTHS="$(echo "${MONTHS}" | awk '{print $2}')"

if [ "${NCLOSES:-0}" -lt 80 ] || [ "${NMONTHS:-0}" -lt 2 ]; then
  log "thin replay (${NCLOSES} closes, ${NMONTHS} months) — appending reference ${REFERENCE_RUN}"
  {
    echo "New run ${RID} had ${NCLOSES} closes across ${NMONTHS} month(s)."
    echo "Full-window reference forensics below (prior validated run)."
    echo ""
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/diagnose_oos_replay_coverage.py "${REFERENCE_RUN}"
    echo ""
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/analyze_oos_validation_run.py "${REFERENCE_RUN}" "${WINDOW}_reference"
    echo ""
    sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
      python /tmp/analyze_trade_forensics.py --run-id "${REFERENCE_RUN}" \
      --date-from "${DATE_FROM}" --date-to "${DATE_TO}" --top 15
  } > "${TMP}" 2>&1
  write_section "Reference full-window (${REFERENCE_RUN})" "${TMP}"
  REF_CSV="${REPORT_DIR}/trades_${REFERENCE_RUN}.csv"
  sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/analyze_trade_forensics.py --run-id "${REFERENCE_RUN}" \
    --date-from "${DATE_FROM}" --date-to "${DATE_TO}" --csv "/tmp/trades_ref.csv" >/dev/null 2>&1 || true
  sudo docker cp "option_trading-dashboard-1:/tmp/trades_ref.csv" "${REF_CSV}" 2>/dev/null || true
fi

log "report written: ${REPORT_FILE}"
echo "${RID}" > "${REPORT_DIR}/latest_run_id.txt"
tail -80 "${REPORT_FILE}"

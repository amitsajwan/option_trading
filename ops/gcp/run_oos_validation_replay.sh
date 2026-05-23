#!/usr/bin/env bash
# Queue and analyze OOS validation replay for ML_ENTRY primary voter (frozen config).
#
# Usage:
#   sudo bash ops/gcp/run_oos_validation_replay.sh oos_primary
#   sudo bash ops/gcp/run_oos_validation_replay.sh oos_secondary
#   sudo bash ops/gcp/run_oos_validation_replay.sh in_sample_sanity
#   sudo bash ops/gcp/run_oos_validation_replay.sh all
#   sudo bash ops/gcp/run_oos_validation_replay.sh analyze_only [RUN_ID] [label]
#   sudo bash ops/gcp/run_oos_validation_replay.sh compare /tmp/oos_validation_runs.json
#
set -euo pipefail

REPO="${REPO_ROOT:-/opt/option_trading}"
MODE="${1:-oos_primary}"
RUNS_JSON="${OOS_RUNS_JSON:-/tmp/oos_validation_runs.json}"
RUN_ID_ARG="${2:-}"
LABEL_ARG="${3:-}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

COMPOSE_BASE=(sudo docker compose --env-file "${REPO}/.env.compose" -f "${REPO}/docker-compose.yml")
if [ -f "${REPO}/docker-compose.gcp.yml" ]; then
  COMPOSE_BASE+=(-f "${REPO}/docker-compose.gcp.yml")
fi
HIST_SERVICE=strategy_app_historical
DASH_CONTAINER="${DASH_CONTAINER:-option_trading-dashboard-1}"

log() { echo "[$(date -Is)] $*"; }

window_dates() {
  case "$1" in
    oos_primary)     echo "2024-05-01 2024-07-31" ;;
    oos_secondary)   echo "2023-05-01 2023-07-31" ;;
    in_sample_sanity) echo "2024-08-01 2024-10-31" ;;
    all) echo "" ;;
    *) echo "Unknown mode: $1 (use oos_primary|oos_secondary|in_sample_sanity|all|analyze_only|compare)" >&2; exit 2 ;;
  esac
}

ALL_WINDOWS=(oos_primary oos_secondary in_sample_sanity)

wait_run() {
  local run_id="$1"
  local label="$2"
  log "waiting for eval run ${run_id} (${label})"
  for _ in $(seq 1 600); do
    status="$("${PY}" - <<PY
import json, urllib.request
run_id = "${run_id}"
try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:8008/api/strategy/evaluation/runs/{run_id}",
        timeout=15,
    ) as r:
        d = json.load(r)
    print(str(d.get("status") or "unknown").strip().lower())
except Exception:
    print("pending")
PY
)"
    log "  status=${status}"
    case "${status}" in
      completed|failed|cancelled) return 0 ;;
    esac
    sleep 30
  done
  log "WARN: timeout waiting for replay"
}

queue_replay_run_id() {
  "${PY}" "${REPO}/ops/gcp/queue_replay.py" "${DATE_FROM}" "${DATE_TO}" | "${PY}" -c "
import json, sys
raw = sys.stdin.read().strip()
d = json.loads(raw) if raw.startswith('{') else {}
print(d.get('run_id') or '')
"
}

latest_run_id() {
  "${PY}" - <<'PY'
import json, urllib.request
with urllib.request.urlopen(
    "http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical",
    timeout=15,
) as r:
    d = json.load(r)
run = d if isinstance(d, dict) and d.get("run_id") else d.get("run") or d
print(run.get("run_id") or "")
PY
}

analyze_run() {
  local rid="$1"
  local label="$2"
  log "analyze ${rid} (${label})"
  sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_run.py" "${DASH_CONTAINER}:/tmp/analyze_oos_validation_run.py"
  sudo docker exec -e OOS_LABEL="${label}" "${DASH_CONTAINER}" \
    python /tmp/analyze_oos_validation_run.py "${rid}" "${label}"
}

run_compare() {
  local json_path="$1"
  sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_compare.py" "${DASH_CONTAINER}:/tmp/analyze_oos_validation_compare.py"
  sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_run.py" "${DASH_CONTAINER}:/tmp/analyze_oos_validation_run.py"
  sudo docker cp "${json_path}" "${DASH_CONTAINER}:/tmp/oos_runs.json"
  sudo docker exec -w /tmp "${DASH_CONTAINER}" python /tmp/analyze_oos_validation_compare.py /tmp/oos_runs.json
}

if [ "${MODE}" = "analyze_only" ]; then
  RID="${RUN_ID_ARG:-$(latest_run_id)}"
  LABEL="${LABEL_ARG:-oos}"
  analyze_run "${RID}" "${LABEL}"
  exit 0
fi

if [ "${MODE}" = "compare" ]; then
  JSON_PATH="${RUN_ID_ARG:-${RUNS_JSON}}"
  log "compare three windows from ${JSON_PATH}"
  run_compare "${JSON_PATH}"
  exit 0
fi

wait_consumers_only() {
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 180 || true
  sleep 10
}

setup_frozen_env() {
  log "frozen: trader_master_ml_entry_det_dir_v1, ENTRY_ML_MIN_PROB=0.65, commit a133936+"
  export ENTRY_ML_MIN_PROB=0.65
  if [ -w "${REPO}/.env.compose" ]; then
    bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh" "${REPO}/.env.compose"
    bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${REPO}/.env.compose"
  else
    sudo bash "${REPO}/ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh" "${REPO}/.env.compose"
    sudo bash "${REPO}/ops/gcp/patch_trader_master_eval_replay_env.sh" "${REPO}/.env.compose"
  fi
  log "rebuild ${HIST_SERVICE}"
  cd "${REPO}"
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" clear --force 2>/dev/null || true
  "${COMPOSE_BASE[@]}" build "${HIST_SERVICE}"
  "${COMPOSE_BASE[@]}" up -d --force-recreate --pull never "${HIST_SERVICE}"
  sleep 15
  "${PY}" "${REPO}/ops/gcp/wait_historical_consumers.py" 180 || true
}

run_single_window() {
  local label="$1"
  read -r DATE_FROM DATE_TO <<< "$(window_dates "${label}")"
  log "=== window ${label} (${DATE_FROM} -> ${DATE_TO}) ==="
  bash "${REPO}/ops/gcp/clean_state_before_replay.sh"
  "${PY}" "${REPO}/ops/gcp/preflight_historical_replay.py"
  RID="$(queue_replay_run_id)"
  if [ -z "${RID}" ]; then
    log "ERROR: queue_replay did not return run_id for ${label}"
    return 1
  fi
  log "queued ${label} run_id=${RID}"
  wait_run "${RID}" "${label}"
  emitted="$("${PY}" - <<PY
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8008/api/strategy/evaluation/runs/${RID}", timeout=15) as r:
    d = json.load(r)
msg = str(d.get("message") or "")
import re
m = re.search(r"emitted=(\\d+)", msg)
print(m.group(1) if m else "0")
PY
)"
  if [ "${label}" = "oos_secondary" ] && [ "${emitted:-0}" = "0" ]; then
    log "SKIP ${label}: no snapshots emitted (2023 parquet missing on VM)"
    sudo "${PY}" -c "
import json
p='${RUNS_JSON}'
d=json.load(open(p)) if __import__('os').path.exists(p) else {}
d['${label}']='${RID}'
d['${label}_skipped']='no_parquet'
json.dump(d, open(p,'w'), indent=2)
"
    return 0
  fi
  analyze_run "${RID}" "${label}"
  sudo "${PY}" -c "
import json
p='${RUNS_JSON}'
try:
    d=json.load(open(p))
except Exception:
    d={}
d['${label}']='${RID}'
json.dump(d, open(p,'w'), indent=2)
print('saved', '${label}', '${RID}')
"
}

if [ "${MODE}" = "replay_only" ]; then
  LABEL="${2:-oos_primary}"
  wait_consumers_only
  run_single_window "${LABEL}"
  exit 0
fi

if [ "${MODE}" = "all" ]; then
  log "=== OOS validation ALL (3 windows) ==="
  setup_frozen_env
  sudo bash -c "echo '{}' > '${RUNS_JSON}'"
  for w in "${ALL_WINDOWS[@]}"; do
    run_single_window "${w}" || log "WARN: ${w} had errors"
  done
  log "=== combined comparison ==="
  run_compare "${RUNS_JSON}" | tee /tmp/oos_validation_compare.log
  log "run ids: $(sudo cat "${RUNS_JSON}")"
  exit 0
fi

setup_frozen_env
run_single_window "${MODE}"
log "done"

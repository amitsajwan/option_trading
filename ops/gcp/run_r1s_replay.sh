#!/usr/bin/env bash
# R1S sell-side hypothesis replay driver — Gates 1, 2, 3.
#
# Pre-registered gates (DO NOT CHANGE after first replay result):
#   Gate 1 — IS    : 2020-07-01 → 2023-12-31  PF ≥ 1.30, n ≥ 30 calm trades, CI_lb ≥ 1.00
#   Gate 2 — OOS-A : 2024-01-01 → 2024-06-30  PF ≥ 1.20, n ≥ 20, cap PnL ≥ 0%
#   Gate 3 — OOS-B : 2024-07-01 → 2024-10-31  trade_reduction ≥ 60%, cap PnL ≥ -5%
#
# Usage (on VM from /opt/option_trading):
#   sudo bash ops/gcp/run_r1s_replay.sh gate1
#   sudo bash ops/gcp/run_r1s_replay.sh gate2
#   sudo bash ops/gcp/run_r1s_replay.sh gate3
#   sudo bash ops/gcp/run_r1s_replay.sh gate1_unfiltered   # baseline without VIX filter
#
# Profile required: trader_r1s_v1 (short ATM CE, VIX-filtered)
# See docs/R1S_SELLSIDE_HYPOTHESIS_2026-05-26.md for frozen spec.
#
set -euo pipefail

REPO="${REPO_ROOT:-/opt/option_trading}"
GATE="${1:-gate1}"
LOG_DIR="${REPO}/.run/r1s_replay"
mkdir -p "${LOG_DIR}"

if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

COMPOSE_BASE=(sudo docker compose --env-file "${REPO}/.env.compose" -f "${REPO}/docker-compose.yml")
if [ -f "${REPO}/docker-compose.gcp.yml" ]; then
  COMPOSE_BASE+=(-f "${REPO}/docker-compose.gcp.yml")
fi

log() { echo "[$(date -Is)] $*" | tee -a "${LOG_DIR}/master.log"; }

gate_dates() {
  case "$1" in
    gate1)             echo "2020-07-01 2023-12-31" ;;
    gate1_unfiltered)  echo "2020-07-01 2023-12-31" ;;
    gate2)             echo "2024-01-01 2024-06-30" ;;
    gate3)             echo "2024-07-01 2024-10-31" ;;
    *) log "ERROR: unknown gate '$1' (use gate1|gate1_unfiltered|gate2|gate3)"; exit 2 ;;
  esac
}

gate_profile() {
  case "$1" in
    gate1_unfiltered)  echo "trader_r1s_v1_unfiltered" ;;
    *)                 echo "trader_r1s_v1" ;;
  esac
}

wait_hist() {
  log "waiting for historical strategy service to be idle..."
  for _ in $(seq 1 120); do
    if "${COMPOSE_BASE[@]}" ps strategy_app_historical 2>/dev/null | grep -q "Up"; then
      sleep 5
    else
      sleep 2
      return 0
    fi
  done
  log "WARN: timed out waiting for strategy_app_historical to go idle"
}

queue_run() {
  local from="$1" to="$2"
  "${PY}" "${REPO}/ops/gcp/queue_replay.py" "${from}" "${to}" | "${PY}" -c "
import json, sys
raw = sys.stdin.read().strip()
d = json.loads(raw) if raw.startswith('{') else {}
print(d.get('run_id') or '')
"
}

wait_run() {
  local run_id="$1"
  log "polling run_id=${run_id}..."
  for _ in $(seq 1 720); do
    status="$("${PY}" - <<PY
import json, urllib.request
try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:8008/api/strategy/evaluation/runs/${run_id}",
        timeout=15,
    ) as r:
        d = json.load(r)
    print(str(d.get("status") or "unknown").strip().lower())
except Exception:
    print("pending")
PY
)"
    log "  run_id=${run_id} status=${status}"
    case "${status}" in
      completed|failed|cancelled) return 0 ;;
    esac
    sleep 30
  done
  log "WARN: timeout waiting for replay run_id=${run_id}"
}

analyze_run() {
  local run_id="$1" gate="$2" log_file="$3"
  log "analyzing ${gate} run_id=${run_id}"
  "${PY}" "${REPO}/ops/gcp/analyze_oos_validation_run.py" \
    --run-id "${run_id}" \
    --label "R1S_${gate}" \
    2>&1 | tee -a "${log_file}"
}

# ── main ──────────────────────────────────────────────────────────────────────
read -r DATE_FROM DATE_TO <<< "$(gate_dates "${GATE}")"
PROFILE="$(gate_profile "${GATE}")"
LOG_FILE="${LOG_DIR}/r1s_${GATE}.log"

log "=== R1S replay: ${GATE} | ${DATE_FROM} → ${DATE_TO} | profile=${PROFILE} ==="
log "Hypothesis doc: docs/R1S_SELLSIDE_HYPOTHESIS_2026-05-26.md"

log "--- patching strategy profile to ${PROFILE} ---"
export TRADER_PROFILE="${PROFILE}"
export REPLAY_EMIT_SNAPS_PER_MIN="${REPLAY_EMIT_SNAPS_PER_MIN:-2400}"

log "--- preflight ---"
"${PY}" "${REPO}/ops/gcp/preflight_historical_replay.py" 2>&1 | tee -a "${LOG_FILE}" || true

log "--- clean state ---"
bash "${REPO}/ops/gcp/clean_state_before_replay.sh" 2>&1 | tee -a "${LOG_FILE}"

wait_hist

log "--- queueing replay ${DATE_FROM} → ${DATE_TO} ---"
RID="$(queue_run "${DATE_FROM}" "${DATE_TO}")"
if [ -z "${RID}" ]; then
  log "ERROR: queue_replay did not return a run_id — aborting"
  exit 1
fi
log "run_id=${RID}"
echo "${RID}" > "${LOG_DIR}/r1s_${GATE}_run_id.txt"

wait_run "${RID}"

log "--- analysis ---"
analyze_run "${RID}" "${GATE}" "${LOG_FILE}"

log "=== R1S ${GATE} complete. run_id=${RID} ==="
log "Results log: ${LOG_FILE}"
log "Next step:"
case "${GATE}" in
  gate1)
    log "  If IS calm PF >= 1.30 AND n >= 30 AND CI_lb >= 1.00 AND high-vol reduction >= 60%:"
    log "  → run: sudo bash ops/gcp/run_r1s_replay.sh gate2"
    log "  Else: hypothesis falsified — do NOT proceed to OOS."
    ;;
  gate2)
    log "  If OOS-A PF >= 1.20 AND n >= 20 AND cap_pnl >= 0%:"
    log "  → run: sudo bash ops/gcp/run_r1s_replay.sh gate3"
    log "  Else: hypothesis falsified."
    ;;
  gate3)
    log "  If high-vol reduction >= 60% AND cap_pnl >= -5%:"
    log "  → ALL 3 GATES PASSED — proceed to R1-S5 engine implementation story."
    log "  Else: hypothesis falsified."
    ;;
  gate1_unfiltered)
    log "  Baseline (no VIX filter) — compare trade count and PF vs gate1 to measure filter impact."
    ;;
esac

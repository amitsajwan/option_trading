#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
BRANCH="${BRANCH:-chore/ml-pipeline-ubuntu-gcp-runbook}"
SESSION_NAME="${SESSION_NAME:-det_research_suite}"
DATE_FROM="${DATE_FROM:-2023-01-01}"
DATE_TO="${DATE_TO:-2024-03-28}"
ANCHOR_DATE_FROM="${ANCHOR_DATE_FROM:-2022-01-01}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/deterministic_research_suite/overnight_suite}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
EXPORT_PROFILE_TRADES="${EXPORT_PROFILE_TRADES:-det_core_v2 det_prod_v1 det_setup_v1 det_v3_v1}"

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

require_command git
require_command tmux

if [ ! -d "${REPO_ROOT}" ]; then
  echo "Repo root not found: ${REPO_ROOT}" >&2
  exit 1
fi

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Python binary not found or not executable: ${PYTHON_BIN}" >&2
  exit 1
fi

mkdir -p "$(dirname "${OUTPUT_DIR}")"

cd "${REPO_ROOT}"
git fetch origin
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

HEAD_LINE="$(git log -1 --oneline)"
RUN_LOG="${OUTPUT_DIR}.run.log"
SUMMARY_FILE="${OUTPUT_DIR}.summary.txt"
STATUS_FILE="${OUTPUT_DIR}.status.txt"

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session already exists: ${SESSION_NAME}" >&2
  echo "Use a different SESSION_NAME or close the old session first." >&2
  exit 1
fi

rm -rf "${OUTPUT_DIR}"
rm -f "${RUN_LOG}" "${SUMMARY_FILE}" "${STATUS_FILE}" "${OUTPUT_DIR}.job.sh"

cat > "${OUTPUT_DIR}.job.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
trap 'echo "FAILED \$(date -Is)" | tee -a "${STATUS_FILE}"' ERR
cd "${REPO_ROOT}"
echo "START \$(date -Is)" | tee "${STATUS_FILE}"
echo "HEAD ${HEAD_LINE}" | tee -a "${STATUS_FILE}"
"${PYTHON_BIN}" -m strategy_app.tools.deterministic_research_suite \\
  --date-from "${DATE_FROM}" \\
  --date-to "${DATE_TO}" \\
  --anchor-date-from "${ANCHOR_DATE_FROM}" \\
  --output-dir "${OUTPUT_DIR}" \\
  --export-profile-trades ${EXPORT_PROFILE_TRADES} | tee "${RUN_LOG}"
{
  echo "HEAD ${HEAD_LINE}"
  echo
  echo "== suite_recommendation.json =="
  cat "${OUTPUT_DIR}/suite_recommendation.json"
  echo
  echo "== scenario_recommendations.csv =="
  cat "${OUTPUT_DIR}/scenario_recommendations.csv"
  echo
  echo "== suite_profile_leaderboard.csv =="
  cat "${OUTPUT_DIR}/suite_profile_leaderboard.csv"
  echo
  echo "== follow_up =="
  cat "${OUTPUT_DIR}/suite_follow_up.json"
  echo
  echo "END \$(date -Is)"
} > "${SUMMARY_FILE}"
echo "DONE \$(date -Is)" | tee -a "${STATUS_FILE}"
EOF

chmod +x "${OUTPUT_DIR}.job.sh"

tmux new-session -d -s "${SESSION_NAME}" "bash '${OUTPUT_DIR}.job.sh'"

echo "Started tmux session: ${SESSION_NAME}"
echo "Head: ${HEAD_LINE}"
echo "Output dir: ${OUTPUT_DIR}"
echo "Run log: ${RUN_LOG}"
echo "Summary: ${SUMMARY_FILE}"
echo
echo "Morning checks:"
echo "  tmux attach -t ${SESSION_NAME}"
echo "  cat ${STATUS_FILE}"
echo "  cat ${SUMMARY_FILE}"

#!/usr/bin/env bash
# D1-S2: Run direction feature audit over live snapshot data.
#
# Usage:
#   bash ops/gcp/run_direction_audit.sh                      # today only
#   bash ops/gcp/run_direction_audit.sh 2026-05-26 2026-06-02  # custom range
#
# Output:
#   docs/audits/CHAIN_FEATURES_DIRECTION_AUDIT_<DATE>.md
#
set -euo pipefail

REPO="${REPO_ROOT:-/opt/option_trading}"
if [ -x "${REPO}/.venv/bin/python3" ]; then
  PY="${REPO}/.venv/bin/python3"
else
  PY="$(command -v python3)"
fi

TODAY="$(date +%Y-%m-%d)"
START="${1:-$(date -d '30 days ago' +%Y-%m-%d 2>/dev/null || date -v-30d +%Y-%m-%d)}"
END="${2:-${TODAY}}"
REPORT="${REPO}/docs/audits/CHAIN_FEATURES_DIRECTION_AUDIT_${TODAY}.md"

echo "[$(date -Is)] Running direction feature audit: ${START} → ${END}"
echo "[$(date -Is)] Report will be saved to: ${REPORT}"

cd "${REPO}"
"${PY}" docs/audits/direction_audit_template.py \
  --start "${START}" \
  --end "${END}" \
  --mongo "${MONGODB_URI:-mongodb://mongo:27017}" \
  --db "${MONGO_DB:-trading_ai}" \
  --save "${REPORT}"

echo "[$(date -Is)] Done. Audit report: ${REPORT}"

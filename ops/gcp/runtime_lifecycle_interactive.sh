#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"

echo "Option trading GCP lifecycle"
echo "1) Bootstrap infra (create/update VM + buckets + config)"
echo "2) Start/restart runtime (interactive deploy + optional Kite auth)"
echo "3) Stop runtime VM"
echo "4) Destroy infra (preserve data buckets + images)"
echo
read -r -p "Choose action [1-4]: " action || true

case "${action}" in
  1)
    bash "${REPO_ROOT}/ops/gcp/bootstrap_runtime_interactive.sh"
    ;;
  2)
    bash "${REPO_ROOT}/ops/gcp/start_runtime_interactive.sh"
    ;;
  3)
    bash "${REPO_ROOT}/ops/gcp/stop_runtime.sh"
    ;;
  4)
    bash "${REPO_ROOT}/ops/gcp/destroy_infra_preserve_data.sh"
    ;;
  *)
    echo "Invalid action: ${action}" >&2
    exit 1
    ;;
esac

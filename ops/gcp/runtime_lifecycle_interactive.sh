#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"

echo "Option trading GCP lifecycle"
echo "1) Bootstrap infra (create/update VM + buckets + config)"
echo "2) Start/restart runtime (interactive deploy + optional Kite auth)"
echo "3) Historical replay (interactive sync + historical compose + replay)"
echo "4) Stop runtime VM"
echo "5) Destroy infra (preserve data buckets + images)"
echo "6) Start training (interactive full/test/HPO/grid modes)"
echo
read -r -p "Choose action [1-6]: " action || true

case "${action}" in
  1)
    bash "${REPO_ROOT}/ops/gcp/bootstrap_runtime_interactive.sh"
    ;;
  2)
    bash "${REPO_ROOT}/ops/gcp/start_runtime_interactive.sh"
    ;;
  3)
    bash "${REPO_ROOT}/ops/gcp/start_historical_interactive.sh"
    ;;
  4)
    bash "${REPO_ROOT}/ops/gcp/stop_runtime.sh"
    ;;
  5)
    bash "${REPO_ROOT}/ops/gcp/destroy_infra_preserve_data.sh"
    ;;
  6)
    bash "${REPO_ROOT}/ops/gcp/start_training_interactive.sh"
    ;;
  *)
    echo "Invalid action: ${action}" >&2
    exit 1
    ;;
esac

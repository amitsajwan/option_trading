#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper for older notes that referenced this file.
# The supported velocity deploy flow is documented in ops/gcp/VELOCITY_RUNTIME_DEPLOY.md.

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

echo "Deploying velocity features through the GCP VM Docker Compose path."
echo "Runbook: ${REPO_ROOT}/ops/gcp/VELOCITY_RUNTIME_DEPLOY.md"
echo

exec bash "${REPO_ROOT}/quick_deploy_gcp.sh"

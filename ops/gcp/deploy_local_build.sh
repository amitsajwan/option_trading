#!/usr/bin/env bash
# deploy_local_build.sh
#
# Full local-build deploy: pull latest code, rebuild ALL local services,
# restart everything in one shot. Run this after any code push to ensure
# every container is on the same commit — no piecemeal rebuilds.
#
# Usage (on the runtime VM):
#   cd /opt/option_trading
#   sudo bash ops/gcp/deploy_local_build.sh [branch]
#
# Default branch: current HEAD (no pull). Pass a branch name to pull first.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRANCH="${1:-}"
COMPOSE="docker compose --env-file .env.compose -f docker-compose.yml"

cd "${REPO_ROOT}"

# 1. Pull latest code if branch specified
if [ -n "${BRANCH}" ]; then
  echo "=== Pull ${BRANCH} ==="
  git fetch origin "${BRANCH}"
  git checkout "${BRANCH}"
  git pull origin "${BRANCH}"
fi

echo "=== Current commit: $(git log --oneline -1) ==="

# 2. Rebuild ALL locally-built services in one shot
echo "=== Rebuild all local images ==="
${COMPOSE} build \
  strategy_app \
  strategy_app_sim \
  dashboard \
  snapshot_app \
  ingestion_app \
  strategy_persistence_app \
  depth_collector

# 3. Restart live services (sim containers are spawned on-demand; skip here)
echo "=== Restart live services ==="
${COMPOSE} up -d \
  strategy_app \
  dashboard \
  snapshot_app \
  ingestion_app \
  strategy_persistence_app

echo ""
echo "=== Deploy complete — commit: $(git log --oneline -1) ==="
echo "=== Container status ==="
${COMPOSE} ps --format "table {{.Name}}\t{{.Image}}\t{{.Status}}" 2>/dev/null | grep -v sim | grep -v historical

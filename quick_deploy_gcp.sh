#!/usr/bin/env bash
set -euo pipefail

# Quick velocity deploy for the GCP runtime VM.
# This repo's live runtime deploy path is Docker Compose on the VM, not Cloud Run.

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env.compose}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8008}"
RUN_GIT_PULL="${RUN_GIT_PULL:-1}"
RUN_TESTS="${RUN_TESTS:-1}"

cd "${REPO_ROOT}"

docker_cmd=(docker)
if ! docker ps >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    docker_cmd=(sudo docker)
  fi
fi

set_env_key() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

echo "== Velocity deploy: VM Compose path =="
echo "repo: ${REPO_ROOT}"

if [ "${RUN_GIT_PULL}" = "1" ]; then
  git fetch origin main
  git checkout main
  git pull --ff-only origin main
fi

if [ ! -f "${ENV_FILE}" ]; then
  cp "${REPO_ROOT}/.env.compose.example" "${ENV_FILE}"
fi

set_env_key STRATEGY_ENHANCED_VELOCITY 1
set_env_key IMAGE_SOURCE local_build

if [ "${RUN_TESTS}" = "1" ]; then
  python3 -m pip install pytest pandas numpy -q
  python3 -m pytest strategy_app/engines/test_velocity_policies.py -q
fi

"${docker_cmd[@]}" compose --env-file "${ENV_FILE}" -f docker-compose.yml build strategy_app dashboard
"${docker_cmd[@]}" compose --env-file "${ENV_FILE}" -f docker-compose.yml --profile ui up -d strategy_app dashboard
"${docker_cmd[@]}" compose --env-file "${ENV_FILE}" -f docker-compose.yml --profile ui ps

echo
echo "Health:"
curl -fsS "http://127.0.0.1:${DASHBOARD_PORT}/api/health" || true
echo
echo "Dashboard:"
echo "http://<runtime-vm-external-ip>:${DASHBOARD_PORT}/trading/velocity-testing"

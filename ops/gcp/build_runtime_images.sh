#!/usr/bin/env bash
set -euo pipefail

# NOTE: This script builds images to Artifact Registry via Cloud Build.
# The live runtime uses GHCR-published images (IMAGE_SOURCE=ghcr).
# Run this only when explicitly targeting the Artifact Registry path for
# infra-compatibility builds. For the live deploy path, publish images to
# GHCR through the standard CI/CD workflow instead.

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:?set REGION}"
REPOSITORY="${REPOSITORY:?set REPOSITORY}"
TAG="${TAG:?set TAG}"

REGISTRY_HOST="${REGION}-docker.pkg.dev"

declare -A DOCKERFILES=(
  [ingestion_app]="ingestion_app/Dockerfile"
  [snapshot_app]="snapshot_app/Dockerfile"
  [persistence_app]="persistence_app/Dockerfile"
  [strategy_app]="strategy_app/Dockerfile"
  [market_data_dashboard]="market_data_dashboard/Dockerfile"
  [strategy_eval_orchestrator]="strategy_eval_orchestrator/Dockerfile"
  [strategy_eval_ui]="strategy_eval_ui/Dockerfile"
)

if [ "$#" -eq 0 ]; then
  SERVICES=(
    ingestion_app
    snapshot_app
    persistence_app
    strategy_app
    market_data_dashboard
    strategy_eval_orchestrator
    strategy_eval_ui
  )
else
  SERVICES=("$@")
fi

for service in "${SERVICES[@]}"; do
  dockerfile="${DOCKERFILES[$service]:-}"
  if [ -z "${dockerfile}" ]; then
    echo "Unknown service: ${service}" >&2
    exit 1
  fi

  image="${REGISTRY_HOST}/${PROJECT_ID}/${REPOSITORY}/${service}:${TAG}"
  echo "Building ${service} -> ${image}"
  build_config="$(mktemp)"
  cat > "${build_config}" <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args:
      - build
      - -f
      - ${dockerfile}
      - -t
      - ${image}
      - .
images:
  - ${image}
EOF

  gcloud builds submit . \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --config "${build_config}"

  rm -f "${build_config}"
done

echo "Build complete."

#!/usr/bin/env bash
set -euo pipefail

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
  gcloud builds submit . \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --file "${dockerfile}" \
    --tag "${image}"
done

echo "Build complete."

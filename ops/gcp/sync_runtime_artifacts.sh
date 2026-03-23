#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
RUNTIME_CONFIG_SOURCE="${RUNTIME_CONFIG_SOURCE:-}"
PUBLISHED_MODELS_SOURCE="${PUBLISHED_MODELS_SOURCE:-}"
DATA_SOURCE="${DATA_SOURCE:-}"

mkdir -p "${REPO_ROOT}/.deploy/runtime-config"
mkdir -p "${REPO_ROOT}/ml_pipeline_2/artifacts/published_models"
mkdir -p "${REPO_ROOT}/.data/ml_pipeline"

if [ -n "${RUNTIME_CONFIG_SOURCE}" ]; then
  gcloud storage rsync "${RUNTIME_CONFIG_SOURCE}" "${REPO_ROOT}/.deploy/runtime-config" --recursive
  if [ -f "${REPO_ROOT}/.deploy/runtime-config/.env.compose" ]; then
    cp "${REPO_ROOT}/.deploy/runtime-config/.env.compose" "${REPO_ROOT}/.env.compose"
  fi
  if [ -f "${REPO_ROOT}/.deploy/runtime-config/ingestion_app/credentials.json" ]; then
    mkdir -p "${REPO_ROOT}/ingestion_app"
    cp "${REPO_ROOT}/.deploy/runtime-config/ingestion_app/credentials.json" "${REPO_ROOT}/ingestion_app/credentials.json"
    chmod 600 "${REPO_ROOT}/ingestion_app/credentials.json"
  fi
fi

if [ -n "${PUBLISHED_MODELS_SOURCE}" ]; then
  gcloud storage rsync "${PUBLISHED_MODELS_SOURCE}" "${REPO_ROOT}/ml_pipeline_2/artifacts/published_models" --recursive
fi

if [ -n "${DATA_SOURCE}" ]; then
  gcloud storage rsync "${DATA_SOURCE}" "${REPO_ROOT}/.data/ml_pipeline" --recursive
fi

echo "Runtime artifact sync complete."

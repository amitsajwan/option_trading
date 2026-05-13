#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# new_project_setup.sh — One-command setup for a fresh GCP project.
#
# Use this when you have lost your GCP project and are starting fresh,
# or when setting up for the first time.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated: gcloud auth login
#   - Local parquet backup in .data/ml_pipeline/parquet_data/ (from backup_to_local.sh)
#   - Local model artifacts in ml_pipeline_2/artifacts/published_models/
#   - Terraform installed (v1.5+): https://developer.hashicorp.com/terraform/install
#
# Usage:
#   NEW_PROJECT=your-gcp-project-id bash ops/gcp/new_project_setup.sh
#
# Or step by step (skip data upload if already done):
#   SKIP_DATA_UPLOAD=1 NEW_PROJECT=your-project bash ops/gcp/new_project_setup.sh
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NEW_PROJECT="${NEW_PROJECT:-}"
OLD_PROJECT="amittrading-493606"
SKIP_DATA_UPLOAD="${SKIP_DATA_UPLOAD:-0}"
SKIP_TERRAFORM="${SKIP_TERRAFORM:-0}"
SKIP_TRAINING_VM="${SKIP_TRAINING_VM:-0}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠ $*${NC}"; }
fail() { echo -e "${RED}[$(date '+%H:%M:%S')] ✗ $*${NC}"; exit 1; }
hr()   { echo ""; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

hr
log "=== New GCP Project Setup ==="
log "Repo: ${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Step 0: Validate inputs
# ---------------------------------------------------------------------------
hr
if [ -z "${NEW_PROJECT}" ]; then
  fail "Set NEW_PROJECT env var: NEW_PROJECT=my-project-id bash $0"
fi
log "New project: ${NEW_PROJECT}"

if ! gcloud auth print-access-token > /dev/null 2>&1; then
  fail "Not authenticated. Run: gcloud auth login && gcloud auth application-default login"
fi
ok "gcloud authenticated"

# ---------------------------------------------------------------------------
# Step 1: Update operator.env with new project ID
# ---------------------------------------------------------------------------
hr
log "Step 1: Updating operator.env with new project ID..."
OPERATOR_ENV="${REPO_ROOT}/ops/gcp/operator.env"

if [ ! -f "${OPERATOR_ENV}" ]; then
  fail "operator.env not found at ${OPERATOR_ENV}. Copy from ops/gcp/operator.env.example first."
fi

# Replace old project ID everywhere in operator.env
if grep -q "${OLD_PROJECT}" "${OPERATOR_ENV}"; then
  sed -i "s/${OLD_PROJECT}/${NEW_PROJECT}/g" "${OPERATOR_ENV}"
  ok "Replaced ${OLD_PROJECT} -> ${NEW_PROJECT} in operator.env"
else
  warn "Old project ID not found in operator.env — may already be updated"
fi

# Also update PROJECT_ID line explicitly
sed -i "s/^PROJECT_ID=.*/PROJECT_ID=\"${NEW_PROJECT}\"/" "${OPERATOR_ENV}"

# Set IMAGE_SOURCE to ghcr for faster startup (no local build needed)
sed -i 's/^IMAGE_SOURCE=.*/IMAGE_SOURCE="ghcr"/' "${OPERATOR_ENV}"

source "${OPERATOR_ENV}"
log "Project ID: ${PROJECT_ID}"
log "Region: ${REGION}"
log "Model bucket: ${MODEL_BUCKET_NAME}"

# ---------------------------------------------------------------------------
# Step 2: Create GCP project resources (IAM + APIs)
# ---------------------------------------------------------------------------
hr
log "Step 2: Enabling GCP APIs..."
REQUIRED_APIS=(
  compute.googleapis.com
  storage.googleapis.com
  artifactregistry.googleapis.com
  iam.googleapis.com
  cloudresourcemanager.googleapis.com
  serviceusage.googleapis.com
)
for api in "${REQUIRED_APIS[@]}"; do
  gcloud services enable "${api}" --project="${NEW_PROJECT}" --quiet 2>/dev/null && log "  enabled: ${api}" || warn "  already enabled or failed: ${api}"
done
ok "APIs enabled"

# ---------------------------------------------------------------------------
# Step 3: Terraform — create buckets, VM templates, networking
# ---------------------------------------------------------------------------
hr
if [ "${SKIP_TERRAFORM}" = "1" ]; then
  warn "Step 3: SKIP_TERRAFORM=1 — skipping infrastructure creation"
else
  log "Step 3: Running Terraform to create GCP infrastructure..."
  RUN_TERRAFORM=1 RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 \
    TERRAFORM_AUTO_APPROVE=1 \
    REPO_ROOT="${REPO_ROOT}" \
    bash "${REPO_ROOT}/ops/gcp/from_scratch_bootstrap.sh"
  ok "Terraform complete"
fi

# ---------------------------------------------------------------------------
# Step 4: Upload local parquet + models to new GCS buckets
# ---------------------------------------------------------------------------
hr
NEW_SNAPSHOT_BUCKET="${NEW_PROJECT}-option-trading-snapshots"
NEW_MODEL_BUCKET="${NEW_PROJECT}-option-trading-models"
NEW_RUNTIME_CONFIG_BUCKET="${NEW_PROJECT}-option-trading-runtime-config"

LOCAL_PARQUET="${REPO_ROOT}/.data/ml_pipeline/parquet_data"
LOCAL_MODELS="${REPO_ROOT}/ml_pipeline_2/artifacts/published_models"
LOCAL_RUNTIME_CONFIG="${REPO_ROOT}/.deploy/runtime-config"

if [ "${SKIP_DATA_UPLOAD}" = "1" ]; then
  warn "Step 4: SKIP_DATA_UPLOAD=1 — skipping data upload"
else
  log "Step 4: Uploading parquet data to gs://${NEW_SNAPSHOT_BUCKET}/ml_pipeline/parquet_data ..."
  if [ -d "${LOCAL_PARQUET}" ]; then
    gcloud storage rsync "${LOCAL_PARQUET}" \
      "gs://${NEW_SNAPSHOT_BUCKET}/ml_pipeline/parquet_data" \
      --recursive --project="${NEW_PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
    ok "Parquet upload complete"
  else
    warn "Local parquet not found at ${LOCAL_PARQUET} — skipping"
  fi

  log "Uploading published models to gs://${NEW_MODEL_BUCKET}/published_models ..."
  if [ -d "${LOCAL_MODELS}" ]; then
    gcloud storage rsync "${LOCAL_MODELS}" \
      "gs://${NEW_MODEL_BUCKET}/published_models" \
      --recursive --project="${NEW_PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
    ok "Model upload complete"
  else
    warn "Local models not found at ${LOCAL_MODELS} — skipping"
  fi

  log "Uploading runtime config to gs://${NEW_RUNTIME_CONFIG_BUCKET}/runtime ..."
  if [ -d "${LOCAL_RUNTIME_CONFIG}" ]; then
    gcloud storage rsync "${LOCAL_RUNTIME_CONFIG}" \
      "gs://${NEW_RUNTIME_CONFIG_BUCKET}/runtime" \
      --recursive --project="${NEW_PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
    ok "Runtime config upload complete"
  else
    warn "Local runtime config not found at ${LOCAL_RUNTIME_CONFIG} — skipping"
  fi
fi

# ---------------------------------------------------------------------------
# Step 5: Create training VM and verify parquet sync
# ---------------------------------------------------------------------------
hr
if [ "${SKIP_TRAINING_VM}" = "1" ]; then
  warn "Step 5: SKIP_TRAINING_VM=1 — skipping training VM creation"
else
  log "Step 5: Creating training VM..."
  bash "${REPO_ROOT}/ops/gcp/create_training_vm.sh" || warn "Training VM creation failed — check logs"
  ok "Training VM ready"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
hr
ok "=== Setup Complete ==="
echo ""
echo "New project: ${NEW_PROJECT}"
echo "GCS buckets:"
echo "  gs://${NEW_SNAPSHOT_BUCKET}"
echo "  gs://${NEW_MODEL_BUCKET}"
echo "  gs://${NEW_RUNTIME_CONFIG_BUCKET}"
echo ""
echo "Next steps:"
echo "  1. Verify training VM is up:"
echo "     gcloud compute instances list --project=${NEW_PROJECT}"
echo ""
echo "  2. Start training (run E2 or smoke test):"
echo "     bash ops/gcp/start_training_interactive.sh"
echo ""
echo "  3. When ready for live runtime:"
echo "     bash ops/gcp/runtime_lifecycle_interactive.sh"
echo ""
echo "  4. Kite credentials must be placed at:"
echo "     ops/gcp/secrets/credentials.json (synced to VM by bootstrap)"
echo ""
echo "See docs/runbooks/RECOVERY_RUNBOOK.md for the full GCP-loss recovery guide."

#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# create_enrichment_vm.sh
#
# Spin up the snapshot-enrichment sprint VM.
# Machine: e2-standard-4 (4 vCPU, 16 GB RAM)
# User:    savitasajwan03
# Project: amittrading  |  Zone: asia-south1-b
#
# Usage:
#   cd option_trading_repo
#   bash ops/gcp/create_enrichment_vm.sh
#
# After VM is up, SSH in and run:
#   bash /tmp/enrichment_bootstrap.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
OPERATOR_ENV="${OPERATOR_ENV:-${REPO_ROOT}/ops/gcp/operator.env}"

# shellcheck disable=SC1090
source "${OPERATOR_ENV}"

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID in operator.env}"
ZONE="${ZONE:?set ZONE in operator.env}"

VM_NAME="option-trading-snapshot-enrichment-01"
MACHINE_TYPE="e2-standard-4"
BOOT_DISK_SIZE="50GB"
IMAGE_FAMILY="ubuntu-2204-lts"
IMAGE_PROJECT="ubuntu-os-cloud"
SERVICE_ACCOUNT="savitasajwan03@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_URL="${REPO_CLONE_URL:?set REPO_CLONE_URL in operator.env}"
REPO_REF="${REPO_REF:-main}"
PARQUET_BUCKET="${SNAPSHOT_PARQUET_BUCKET_URL:?set SNAPSHOT_PARQUET_BUCKET_URL in operator.env}"
DATA_PATH="/home/savitasajwan03/option_trading/.data/ml_pipeline/parquet_data"

echo "═══════════════════════════════════════════════════════"
echo "  Creating enrichment VM: ${VM_NAME}"
echo "  Project: ${PROJECT_ID}  Zone: ${ZONE}"
echo "  Machine: ${MACHINE_TYPE}  Disk: ${BOOT_DISK_SIZE}"
echo "═══════════════════════════════════════════════════════"

# ── Check VM doesn't already exist ────────────────────────────────────────────
if gcloud compute instances describe "${VM_NAME}" \
       --project "${PROJECT_ID}" --zone "${ZONE}" \
       --format="value(name)" 2>/dev/null; then
  echo "VM ${VM_NAME} already exists. To recreate, run:"
  echo "  gcloud compute instances delete ${VM_NAME} --project ${PROJECT_ID} --zone ${ZONE} --quiet"
  exit 0
fi

# ── Create VM ─────────────────────────────────────────────────────────────────
gcloud compute instances create "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --machine-type="${MACHINE_TYPE}" \
  --image-family="${IMAGE_FAMILY}" \
  --image-project="${IMAGE_PROJECT}" \
  --boot-disk-size="${BOOT_DISK_SIZE}" \
  --boot-disk-type="pd-ssd" \
  --scopes="storage-rw,logging-write,monitoring-write" \
  --tags="enrichment-vm" \
  --metadata="enable-oslogin=TRUE"

echo ""
echo "VM created: ${VM_NAME}"
echo "Waiting 30s for SSH to become available..."
sleep 30

# ── Upload bootstrap script ────────────────────────────────────────────────────
BOOTSTRAP=$(mktemp /tmp/enrichment_bootstrap.XXXXXX.sh)
cat > "${BOOTSTRAP}" <<BOOTSTRAP_EOF
#!/usr/bin/env bash
set -euo pipefail
echo "==> Bootstrap: snapshot enrichment VM"

# ── system packages ────────────────────────────────────────────────────────────
sudo apt-get update -q
sudo apt-get install -y python3-pip python3-venv git screen htop

# ── data directory ─────────────────────────────────────────────────────────────
mkdir -p "${DATA_PATH}"
echo "==> Syncing parquet data from GCS..."
gsutil -m rsync -r "${PARQUET_BUCKET}/" "${DATA_PATH}/"
echo "==> Sync complete"

# ── repo ───────────────────────────────────────────────────────────────────────
REPO_DIR="/home/savitasajwan03/option_trading/option_trading_repo"
if [ ! -d "\${REPO_DIR}" ]; then
  mkdir -p /home/savitasajwan03/option_trading
  git clone "${REPO_URL}" "\${REPO_DIR}"
fi
cd "\${REPO_DIR}"
git fetch origin
git checkout "${REPO_REF}"
git pull origin "${REPO_REF}"

# ── Python venv ────────────────────────────────────────────────────────────────
VENV_DIR="\${REPO_DIR}/.venv_enrichment"
if [ ! -d "\${VENV_DIR}" ]; then
  python3 -m venv "\${VENV_DIR}"
fi
source "\${VENV_DIR}/bin/activate"

pip install --upgrade pip wheel
pip install duckdb pyarrow pandas numpy

# Install repo packages
if [ -f snapshot_app/pyproject.toml ]; then
  pip install -e snapshot_app/
elif [ -f snapshot_app/setup.py ]; then
  pip install -e snapshot_app/
fi
if [ -f contracts_app/pyproject.toml ]; then
  pip install -e contracts_app/
fi

echo "==> Bootstrap complete"
echo ""
echo "Run the backfill with:"
echo "  screen -S enrichment"
echo "  source \${VENV_DIR}/bin/activate"
echo "  cd \${REPO_DIR}"
echo "  python -m snapshot_app.historical.enrichment_runner \\"
echo "      --parquet-root ${DATA_PATH} \\"
echo "      --start-date 2020-01-01 \\"
echo "      --end-date   2024-12-31 \\"
echo "      --output-dataset snapshots_ml_flat_v2 \\"
echo "      --workers 4 \\"
echo "      --log-level INFO"
echo ""
echo "Dry-run first (validates 10 dates without writing):"
echo "  python -m snapshot_app.historical.enrichment_runner \\"
echo "      --parquet-root ${DATA_PATH} \\"
echo "      --start-date 2020-01-01 \\"
echo "      --end-date   2024-12-31 \\"
echo "      --workers 1 --dry-run"
echo ""
echo "Push results back to GCS when done:"
echo "  gsutil -m rsync -r ${DATA_PATH}/snapshots_ml_flat_v2/ \\"
echo "      ${PARQUET_BUCKET}/snapshots_ml_flat_v2/"
BOOTSTRAP_EOF

gcloud compute scp "${BOOTSTRAP}" \
  "savitasajwan03@${VM_NAME}:/tmp/enrichment_bootstrap.sh" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}"

gcloud compute ssh "savitasajwan03@${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --command="bash /tmp/enrichment_bootstrap.sh 2>&1 | tee /tmp/enrichment_bootstrap.log"

rm -f "${BOOTSTRAP}"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  VM is ready: ${VM_NAME}"
echo ""
echo "  SSH in:"
echo "    gcloud compute ssh savitasajwan03@${VM_NAME} \\"
echo "        --project ${PROJECT_ID} --zone ${ZONE}"
echo ""
echo "  IMPORTANT — shut down after sprint completes:"
echo "    gcloud compute instances stop ${VM_NAME} \\"
echo "        --project ${PROJECT_ID} --zone ${ZONE}"
echo "    # or delete permanently:"
echo "    gcloud compute instances delete ${VM_NAME} \\"
echo "        --project ${PROJECT_ID} --zone ${ZONE} --quiet"
echo "═══════════════════════════════════════════════════════"

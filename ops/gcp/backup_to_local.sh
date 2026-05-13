#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# backup_to_local.sh — Pull all GCS and VM artifacts to local disk before GCP expiry
#
# Run from the repo root on WSL or a Linux machine with gcloud auth:
#   bash ops/gcp/backup_to_local.sh
#
# The script is resumable — gcloud storage rsync skips already-downloaded files.
# Run it again after interruption to continue.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-${REPO_ROOT}}"   # override to write elsewhere, e.g. an external drive

PROJECT="amittrading-493606"
ZONE="asia-south1-b"
TRAINING_VM="option-trading-ml-01"
TRAINING_VM_USER="savitasajwan03"

SNAPSHOT_BUCKET="gs://amittrading-493606-option-trading-snapshots"
MODEL_BUCKET="gs://amittrading-493606-option-trading-models"
RUNTIME_CONFIG_BUCKET="gs://amittrading-493606-option-trading-runtime-config"

PARQUET_GCS="${SNAPSHOT_BUCKET}/ml_pipeline/parquet_data"
LOCAL_PARQUET="${BACKUP_ROOT}/.data/ml_pipeline/parquet_data"
LOCAL_MODELS="${BACKUP_ROOT}/ml_pipeline_2/artifacts/published_models"
LOCAL_RUNTIME_CONFIG="${BACKUP_ROOT}/.deploy/runtime-config"
LOCAL_RESEARCH="${BACKUP_ROOT}/ml_pipeline_2/artifacts/research"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
hr()  { echo ""; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

hr
log "BACKUP START — writing to: ${BACKUP_ROOT}"
log "GCP project: ${PROJECT}"

# ---------------------------------------------------------------------------
# TIER 1 — CRITICAL: training views (621 MB)
# These are the active training datasets. Without them, training cannot restart.
# ---------------------------------------------------------------------------
hr
log "TIER 1: Active training views (~621 MB)"
for dataset in snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
  dest="${LOCAL_PARQUET}/${dataset}"
  mkdir -p "${dest}"
  log "  syncing ${dataset} ..."
  gcloud storage rsync "${PARQUET_GCS}/${dataset}" "${dest}" \
    --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
  log "  done: ${dataset}"
done

# ---------------------------------------------------------------------------
# TIER 1 — CRITICAL: published models (22 MB) and runtime config (8 KB)
# ---------------------------------------------------------------------------
hr
log "TIER 1: Published models (22 MB)"
mkdir -p "${LOCAL_MODELS}"
gcloud storage rsync "${MODEL_BUCKET}/published_models" "${LOCAL_MODELS}" \
  --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true

log "TIER 1: Runtime config (8 KB)"
mkdir -p "${LOCAL_RUNTIME_CONFIG}"
gcloud storage rsync "${RUNTIME_CONFIG_BUCKET}/runtime" "${LOCAL_RUNTIME_CONFIG}" \
  --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true

# ---------------------------------------------------------------------------
# TIER 2: v2 training views (~956 MB) — next-gen datasets
# ---------------------------------------------------------------------------
hr
log "TIER 2: v2 training views (~956 MB)"
for dataset in snapshots_ml_flat_v2 stage1_entry_view_v2 stage2_direction_view_v2 stage3_recipe_view_v2; do
  dest="${LOCAL_PARQUET}/${dataset}"
  mkdir -p "${dest}"
  log "  syncing ${dataset} ..."
  gcloud storage rsync "${PARQUET_GCS}/${dataset}" "${dest}" \
    --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
  log "  done: ${dataset}"
done

# v3 candidates if they exist
for dataset in snapshots_ml_flat_v2_smoke snapshots_ml_flat_v2_smoke_range \
               stage1_entry_view_v3_candidate stage2_direction_view_v3_candidate \
               stage3_recipe_view_v3_candidate; do
  count=$(gcloud storage ls "${PARQUET_GCS}/${dataset}/" 2>/dev/null | wc -l || echo 0)
  if [ "${count}" -gt 0 ]; then
    dest="${LOCAL_PARQUET}/${dataset}"
    mkdir -p "${dest}"
    log "  syncing ${dataset} ..."
    gcloud storage rsync "${PARQUET_GCS}/${dataset}" "${dest}" \
      --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
  fi
done

# ---------------------------------------------------------------------------
# TIER 2: intermediate parquet (market_base, options, futures, spot) — ~1 GB
# Needed to rebuild stage views without starting from raw archive.
# ---------------------------------------------------------------------------
hr
log "TIER 2: Intermediate parquet (market_base + options + futures + spot + snapshots base) (~1 GB)"
for dataset in market_base futures spot; do
  dest="${LOCAL_PARQUET}/${dataset}"
  mkdir -p "${dest}"
  log "  syncing ${dataset} ..."
  gcloud storage rsync "${PARQUET_GCS}/${dataset}" "${dest}" \
    --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
done

# options is 650 MB — prompt before downloading
log ""
log "  options/ is 650 MB. Downloading..."
dest="${LOCAL_PARQUET}/options"
mkdir -p "${dest}"
gcloud storage rsync "${PARQUET_GCS}/options" "${dest}" \
  --recursive --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true

# ---------------------------------------------------------------------------
# TIER 2: VM research artifacts — summary.json + model files for key runs
# Pull summary.json for all runs (tiny), and full artifacts for C1 + D2 best.
# ---------------------------------------------------------------------------
hr
log "TIER 2: VM research artifacts"
mkdir -p "${LOCAL_RESEARCH}"

# All summary.json files — fast, complete experiment history
log "  pulling all summary.json files from VM ..."
gcloud compute ssh "${TRAINING_VM_USER}@${TRAINING_VM}" \
  --zone="${ZONE}" --project="${PROJECT}" \
  --command="
    find /home/${TRAINING_VM_USER}/option_trading/ml_pipeline_2/artifacts/research \
      -name 'summary.json' -o -name 'run_status.json' -o -name 'grid_summary.json' \
      2>/dev/null | tar czf - -T -
  " 2>/dev/null \
  | tar xzf - --strip-components=5 \
    --directory="${LOCAL_RESEARCH}" \
    2>/dev/null || log "  (some summary files may have been skipped)"

# Full artifacts for C1 (live model), D2 best run, B4 winner, and A2 label fix
KEY_RUNS=(
  "staged_deep_hpo_c1_base_20260429_040848"
  "staged_deep_hpo_d2_high_edge_20260501_040643"
  "staged_grid_feature_s2_v1_20260428T040319Z"
  "staged_label_fix_a2_market_direction"
  "staged_proper_full_v1_20260426_051531"
)
for run in "${KEY_RUNS[@]}"; do
  run_path="/home/${TRAINING_VM_USER}/option_trading/ml_pipeline_2/artifacts/research/${run}"
  exists=$(gcloud compute ssh "${TRAINING_VM_USER}@${TRAINING_VM}" \
    --zone="${ZONE}" --project="${PROJECT}" \
    --command="[ -d '${run_path}' ] && echo yes || echo no" 2>/dev/null || echo no)
  if [ "${exists}" = "yes" ]; then
    log "  pulling full artifacts: ${run}"
    mkdir -p "${LOCAL_RESEARCH}/${run}"
    gcloud compute scp --recurse \
      "${TRAINING_VM_USER}@${TRAINING_VM}:${run_path}/." \
      "${LOCAL_RESEARCH}/${run}/" \
      --zone="${ZONE}" --project="${PROJECT}" 2>&1 | grep -v "^Copying\|^-" || true
  fi
done

# ---------------------------------------------------------------------------
# TIER 3 (OPTIONAL): Raw snapshots — 3.16 GB
# Uncomment to download. Only needed if full parquet rebuild from raw is required.
# ---------------------------------------------------------------------------
# hr
# log "TIER 3: Raw snapshots (3.16 GB) — commented out by default"
# dest="${LOCAL_PARQUET}/snapshots"
# mkdir -p "${dest}"
# gcloud storage rsync "${PARQUET_GCS}/snapshots" "${dest}" \
#   --recursive --project="${PROJECT}"

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------
hr
log "BACKUP COMPLETE"
echo ""
echo "What was backed up:"
echo "  ${LOCAL_PARQUET}/             — parquet training data"
echo "  ${LOCAL_MODELS}/              — published model artifacts (C1 + others)"
echo "  ${LOCAL_RUNTIME_CONFIG}/      — runtime config bundle"
echo "  ${LOCAL_RESEARCH}/            — research run artifacts"
echo ""
echo "Local disk usage:"
du -sh "${BACKUP_ROOT}/.data" 2>/dev/null || true
du -sh "${BACKUP_ROOT}/ml_pipeline_2/artifacts" 2>/dev/null || true
du -sh "${BACKUP_ROOT}/.deploy" 2>/dev/null || true
echo ""
echo "To restore on a new GCP project:"
echo "  1. Update ops/gcp/operator.env with new project/bucket names"
echo "  2. Run: bash ops/gcp/from_scratch_bootstrap.sh"
echo "  3. Upload parquet:  gcloud storage rsync ${LOCAL_PARQUET} <NEW_SNAPSHOT_BUCKET>/ml_pipeline/parquet_data --recursive"
echo "  4. Upload models:   gcloud storage rsync ${LOCAL_MODELS} <NEW_MODEL_BUCKET>/published_models --recursive"

#!/usr/bin/env bash
# force_deploy_research_run.sh
#
# Deploy a completed research run to the live runtime even when the run returns
# HOLD on automated hard gates (e.g. combined profit_factor < 1.5).
#
# Use this when you have a research run with demonstrable regime-specific edge
# and want to deploy it while further research continues in parallel.
#
# What this script does:
#   1. Force-publishes the run locally (sets smoke_allow_non_publishable=true)
#   2. Creates release/ml_pure_runtime.env for the run
#   3. Creates a training-release.json compatible with runtime_release_manifest.py
#   4. Writes .run/gcp_release/current_runtime_release.json (runtime deploy pointer)
#   5. Syncs published model bundle to GCS model bucket
#   6. Publishes the runtime config bundle to GCS runtime-config bucket
#
# After this script completes, run:
#   bash ./ops/gcp/start_runtime_interactive.sh
# on your operator machine to deploy to the live runtime VM.
#
# Usage:
#   RUN_DIR=<abs-path-to-research-run-dir> \
#   MODEL_GROUP=banknifty_futures/h15_tp_auto \
#   PROFILE_ID=openfe_v9_dual \
#   APP_IMAGE_TAG=latest \
#   MODEL_BUCKET_URL=gs://amittrading-493606-option-trading-models/published_models \
#   RUNTIME_CONFIG_BUCKET_URL=gs://amittrading-493606-option-trading-runtime-config/runtime \
#   bash ./ops/gcp/force_deploy_research_run.sh
#
# All env vars can also be set in ops/gcp/operator.env.
# MODEL_BUCKET_URL and RUNTIME_CONFIG_BUCKET_URL default to amittrading-493606 project values.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"

if [ -f "${OPERATOR_ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${OPERATOR_ENV_FILE}"
fi

RUN_DIR="${RUN_DIR:?Set RUN_DIR to the abs path of the completed research run directory}"
MODEL_GROUP="${MODEL_GROUP:-banknifty_futures/h15_tp_auto}"
PROFILE_ID="${PROFILE_ID:-openfe_v9_dual}"
APP_IMAGE_TAG="${APP_IMAGE_TAG:-latest}"
RUNTIME_GUARD_PATH="${RUNTIME_GUARD_PATH:-.run/ml_runtime_guard_live.json}"
MODEL_BUCKET_URL="${MODEL_BUCKET_URL:-gs://amittrading-493606-option-trading-models/published_models}"
RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL:-gs://amittrading-493606-option-trading-runtime-config/runtime}"

if [ ! -d "${RUN_DIR}" ]; then
  echo "ERROR: RUN_DIR not found: ${RUN_DIR}" >&2
  exit 1
fi

if [ ! -f "${RUN_DIR}/summary.json" ]; then
  echo "ERROR: ${RUN_DIR}/summary.json not found — run must be completed (mode=completed)" >&2
  exit 1
fi

if [ ! -d "${VENV_DIR}" ]; then
  echo "ERROR: venv not found at ${VENV_DIR} — run: python3 -m venv ${VENV_DIR} && ${VENV_DIR}/bin/pip install -e ${REPO_ROOT}/ml_pipeline_2" >&2
  exit 1
fi

PYTHON="${VENV_DIR}/bin/python"
RUN_ID="$(basename "${RUN_DIR}")"
RELEASE_ROOT="${RUN_DIR}/release"
RUNTIME_ENV_PATH="${RELEASE_ROOT}/ml_pure_runtime.env"
TRAINING_RELEASE_JSON="${RELEASE_ROOT}/force_training_release.json"

echo "== Force-deploying research run =="
echo "   run_id       : ${RUN_ID}"
echo "   model_group  : ${MODEL_GROUP}"
echo "   profile_id   : ${PROFILE_ID}"
echo "   app_image_tag: ${APP_IMAGE_TAG}"
echo "   model_bucket : ${MODEL_BUCKET_URL}"
echo ""

echo "== Step 1: Force-publish locally =="
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" - <<PY
import sys, json, pathlib
sys.path.insert(0, "${REPO_ROOT}")
from ml_pipeline_2.staged.publish import assess_staged_release_candidate, publish_staged_run

run_dir = "${RUN_DIR}"
model_group = "${MODEL_GROUP}"
profile_id = "${PROFILE_ID}"

assessment = assess_staged_release_candidate(run_dir=run_dir, force_publish_nonpublishable=True)
print("  assessment.publishable:", assessment["publishable"])
print("  blocking (informational):", assessment.get("blocking_reasons", []))

if not assessment["publishable"]:
    print("ERROR: still not publishable after force flag — check run integrity")
    sys.exit(1)

result = publish_staged_run(
    run_dir=run_dir,
    model_group=model_group,
    profile_id=profile_id,
    force_publish_nonpublishable=True,
)
print("  publish_status:", result.get("publish_status"))
paths = result.get("published_paths") or {}
print("  published_paths:")
for k, v in paths.items():
    print(f"    {k}: {v}")

# Write published paths to a temp file for the bash layer
import os
p = pathlib.Path("${RELEASE_ROOT}")
p.mkdir(parents=True, exist_ok=True)
(p / "published_paths.json").write_text(json.dumps(paths, indent=2))
PY

echo ""
echo "== Step 2: Write release/ml_pure_runtime.env =="
mkdir -p "${RELEASE_ROOT}"
cat > "${RUNTIME_ENV_PATH}" <<ENV
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=${RUN_ID}
ML_PURE_MODEL_GROUP=${MODEL_GROUP}
ENV
echo "  wrote: ${RUNTIME_ENV_PATH}"

echo ""
echo "== Step 3: Build training-release.json for runtime manifest =="
"${PYTHON}" - <<PY
import json, pathlib, sys

repo_root = pathlib.Path("${REPO_ROOT}")
run_id = "${RUN_ID}"
model_group = "${MODEL_GROUP}"
profile_id = "${PROFILE_ID}"
release_root = pathlib.Path("${RELEASE_ROOT}")
runtime_env_path = pathlib.Path("${RUNTIME_ENV_PATH}")

published_paths_file = release_root / "published_paths.json"
if not published_paths_file.exists():
    print("ERROR: published_paths.json missing — step 1 may have failed", file=sys.stderr)
    sys.exit(1)
pub = json.loads(published_paths_file.read_text())

threshold_report = pub.get("threshold_report", "")
training_report = pub.get("training_report", "")

if not threshold_report or not training_report:
    print("ERROR: published_paths.json missing threshold_report or training_report", file=sys.stderr)
    sys.exit(1)

payload = {
    "release_status": "published",
    "run_id": run_id,
    "publish": {
        "run_id": run_id,
        "model_group": model_group,
        "profile_id": profile_id,
        "publish_status": "published",
        "publish_kind": "ml_pipeline_2_staged_runtime_bundle_v1",
        "active_group_paths": {
            "threshold_report": threshold_report,
            "training_report": training_report,
        },
    },
    "paths": {
        "runtime_env": str(runtime_env_path.resolve()),
        "release_summary": str((release_root / "release_summary.json").resolve()),
    },
}
out = pathlib.Path("${TRAINING_RELEASE_JSON}")
out.write_text(json.dumps(payload, indent=2))
print("  wrote:", out)
PY

echo ""
echo "== Step 4: Write runtime release manifest and current-release pointer =="
"${PYTHON}" "${REPO_ROOT}/ops/gcp/runtime_release_manifest.py" \
    --repo-root "${REPO_ROOT}" \
    --training-release-json "${TRAINING_RELEASE_JSON}" \
    --app-image-tag "${APP_IMAGE_TAG}" \
    --runtime-guard-path "${RUNTIME_GUARD_PATH}" \
    --runtime-config-bucket-url "${RUNTIME_CONFIG_BUCKET_URL}"

echo ""
echo "== Step 5: Sync published models to GCS =="
REPO_ROOT="${REPO_ROOT}" \
MODEL_BUCKET_URL="${MODEL_BUCKET_URL}" \
MODEL_GROUP="${MODEL_GROUP}" \
bash "${SCRIPT_DIR}/publish_published_models.sh"

echo ""
echo "== Step 6: Publish runtime config bundle to GCS =="
REPO_ROOT="${REPO_ROOT}" \
RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL}" \
bash "${SCRIPT_DIR}/publish_runtime_config.sh"

echo ""
echo "========================================================"
echo "Force deploy complete."
echo "  run_id       : ${RUN_ID}"
echo "  model_group  : ${MODEL_GROUP}"
echo "  current manifests written to: ${REPO_ROOT}/.run/gcp_release/"
echo ""
echo "To deploy to the live runtime VM, run from your operator machine:"
echo "  bash ./ops/gcp/start_runtime_interactive.sh"
echo "========================================================"

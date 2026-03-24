#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
TRAINING_TMUX_SESSION_NAME="${TRAINING_TMUX_SESSION_NAME:-}"
TRAINING_INTERACTIVE_IN_TMUX="${TRAINING_INTERACTIVE_IN_TMUX:-0}"
PYTHON_BIN="${PYTHON_BIN:-}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Missing ${OPERATOR_ENV_FILE}. Run bootstrap_runtime_interactive.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

if [ -z "${PYTHON_BIN}" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "${PYTHON_BIN}" ]; then
  echo "Python is required for training launcher summary/report handling." >&2
  exit 1
fi

if [ -z "${TMUX:-}" ] && [ "${TRAINING_INTERACTIVE_IN_TMUX}" != "1" ]; then
  if command -v tmux >/dev/null 2>&1; then
    session_name="${TRAINING_TMUX_SESSION_NAME:-training_$(date -u +%Y%m%d_%H%M%S)}"
    script_path="${REPO_ROOT}/ops/gcp/start_training_interactive.sh"
    tmux new-session -d -s "${session_name}" "cd \"${REPO_ROOT}\" && TRAINING_INTERACTIVE_IN_TMUX=1 bash \"${script_path}\""
    echo "Started interactive training launcher in tmux session: ${session_name}"
    echo "Attach with: tmux attach -t ${session_name}"
    exit 0
  fi
  echo "tmux not found; continuing in foreground. SSH disconnect can interrupt training." >&2
fi

prompt_var() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="${3:-}"
  local entered=""
  if [ -n "${default_value}" ]; then
    read -r -p "${prompt_text} [${default_value}]: " entered || true
    entered="${entered:-${default_value}}"
  else
    read -r -p "${prompt_text}: " entered || true
  fi
  if [ -z "${entered}" ]; then
    echo "Value required: ${var_name}" >&2
    exit 1
  fi
  printf -v "${var_name}" '%s' "${entered}"
}

prompt_yes_no() {
  local prompt_text="$1"
  local default_answer="${2:-Y}"
  local answer=""
  read -r -p "${prompt_text} [${default_answer}/n]: " answer || true
  answer="${answer:-${default_answer}}"
  if [[ "${answer}" =~ ^[Yy]$ ]]; then
    return 0
  fi
  return 1
}

sanitize_tag() {
  local raw="$1"
  local lowered
  lowered="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')"
  lowered="${lowered// /_}"
  lowered="${lowered//\//_}"
  lowered="${lowered//:/_}"
  lowered="${lowered//[^a-z0-9_.-]/_}"
  lowered="${lowered##_}"
  lowered="${lowered%%_}"
  if [ -z "${lowered}" ]; then
    lowered="run"
  fi
  printf '%s\n' "${lowered}"
}

mode_eta_hint() {
  local mode="$1"
  case "${mode}" in
    publish_full) echo "ETA: ~45-180 minutes (full publish candidate)" ;;
    test_quick) echo "ETA: ~10-30 minutes (quick test/verification run)" ;;
    stage1_hpo) echo "ETA: ~90-240 minutes (research only)" ;;
    deep_search) echo "ETA: ~120-360 minutes (research only)" ;;
    stage2_hpo) echo "ETA: ~90-240 minutes (research only)" ;;
    stage2_edge) echo "ETA: ~60-180 minutes (research only)" ;;
    stage1_diag) echo "ETA: ~30-120 minutes (research only)" ;;
    grid_prod) echo "ETA: ~240-720 minutes (research grid)" ;;
    *) echo "ETA: unknown" ;;
  esac
}

mode_plan_hint() {
  local mode="$1"
  case "${mode}" in
    publish_full) echo "Plan: Stage1 + Stage2 + Stage3, strict publish gates, runtime handoff + runtime config publish." ;;
    test_quick) echo "Plan: Stage1 + Stage2 + Stage3 with test model-group lane, no runtime handoff/publish." ;;
    stage1_hpo) echo "Plan: full staged pipeline using Stage1 HPO manifest; research lane only." ;;
    deep_search) echo "Plan: full staged pipeline using deep-search manifest; research lane only." ;;
    stage2_hpo) echo "Plan: full staged pipeline with expanded Stage2 search; Stage1/3 still run." ;;
    stage2_edge) echo "Plan: full staged pipeline with Stage2 label-edge filtering; Stage1/3 still run." ;;
    stage1_diag) echo "Plan: full staged pipeline with Stage1 diagnostic gates; research lane only." ;;
    grid_prod) echo "Plan: multi-lane grid (prod_v1), rank winner, optional winner publish." ;;
    *) echo "Plan: unknown mode." ;;
  esac
}

print_mode_menu() {
  echo "Training mode:"
  echo "1) publish_full  - full staged run (S1+S2+S3), publish + handoff (non-smoke)"
  echo "2) test_quick    - full staged run (S1+S2+S3) in test lane, no publish/handoff"
  echo "3) stage1_hpo    - full staged run with Stage1 HPO-focused manifest"
  echo "4) deep_search   - full staged run with deep-search manifest"
  echo "5) stage2_hpo    - full staged run with Stage2 HPO-focused manifest"
  echo "6) stage2_edge   - full staged run with Stage2 edge-filter manifest"
  echo "7) stage1_diag   - full staged run with Stage1 diagnostic manifest"
  echo "8) grid_prod     - staged grid prod v1, optional winner publish"
}

echo "Staged training interactive launcher"
echo "This flow standardizes run mode, output paths, and publish/handoff behavior."
echo

print_mode_menu
read -r -p "Choose mode [1-8]: " selected_mode || true

MODE_ID=""
MODE_LABEL=""
STAGED_CONFIG_VALUE=""
APPLY_RUNTIME_HANDOFF_VALUE="0"
PUBLISH_RUNTIME_CONFIG_VALUE="0"
RUN_GRID="0"
PUBLISH_WINNER="0"

case "${selected_mode}" in
  1)
    MODE_ID="publish_full"
    MODE_LABEL="full_publish"
    STAGED_CONFIG_VALUE="${STAGED_CONFIG:-ml_pipeline_2/configs/research/staged_dual_recipe.default.json}"
    APPLY_RUNTIME_HANDOFF_VALUE="1"
    PUBLISH_RUNTIME_CONFIG_VALUE="1"
    ;;
  2)
    MODE_ID="test_quick"
    MODE_LABEL="quick_test"
    STAGED_CONFIG_VALUE="${STAGED_CONFIG:-ml_pipeline_2/configs/research/staged_dual_recipe.default.json}"
    APPLY_RUNTIME_HANDOFF_VALUE="0"
    PUBLISH_RUNTIME_CONFIG_VALUE="0"
    ;;
  3)
    MODE_ID="stage1_hpo"
    MODE_LABEL="stage1_hpo"
    STAGED_CONFIG_VALUE="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_hpo.json"
    ;;
  4)
    MODE_ID="deep_search"
    MODE_LABEL="deep_search"
    STAGED_CONFIG_VALUE="ml_pipeline_2/configs/research/staged_dual_recipe.deep_search.json"
    ;;
  5)
    MODE_ID="stage2_hpo"
    MODE_LABEL="stage2_hpo"
    STAGED_CONFIG_VALUE="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_hpo.json"
    ;;
  6)
    MODE_ID="stage2_edge"
    MODE_LABEL="stage2_edge"
    STAGED_CONFIG_VALUE="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_edge_filter.json"
    ;;
  7)
    MODE_ID="stage1_diag"
    MODE_LABEL="stage1_diag"
    STAGED_CONFIG_VALUE="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_diagnostic.json"
    ;;
  8)
    MODE_ID="grid_prod"
    MODE_LABEL="grid_prod_v1"
    STAGED_CONFIG_VALUE="ml_pipeline_2/configs/research/staged_grid.prod_v1.json"
    RUN_GRID="1"
    ;;
  *)
    echo "Invalid mode: ${selected_mode}" >&2
    exit 1
    ;;
esac

echo
echo "$(mode_eta_hint "${MODE_ID}")"
echo "$(mode_plan_hint "${MODE_ID}")"
echo

default_group="${MODEL_GROUP:-banknifty_futures/h15_tp_auto}"
default_lane="${MODE_LABEL}"
if [ "${MODE_ID}" = "publish_full" ]; then
  default_lane="prod"
fi

prompt_var MODEL_GROUP_BASE_VALUE "Base model group" "${default_group}"
prompt_var PROFILE_ID_VALUE "Profile id" "${PROFILE_ID:-openfe_v9_dual}"
prompt_var CONFIG_PATH_VALUE "Config path" "${STAGED_CONFIG_VALUE}"
prompt_var LANE_TAG_RAW "Parallel lane tag (used for collision-safe naming)" "${default_lane}"

LANE_TAG_VALUE="$(sanitize_tag "${LANE_TAG_RAW}")"
MODEL_GROUP_VALUE="${MODEL_GROUP_BASE_VALUE}"

if [ "${MODE_ID}" = "publish_full" ]; then
  if ! prompt_yes_no "Use base model group exactly for publish lane?" "Y"; then
    MODEL_GROUP_VALUE="${MODEL_GROUP_BASE_VALUE}_${LANE_TAG_VALUE}"
  fi
else
  MODEL_GROUP_VALUE="${MODEL_GROUP_BASE_VALUE}_${LANE_TAG_VALUE}"
fi

if [[ "${MODE_ID}" == "publish_full" ]] && [[ "${CONFIG_PATH_VALUE}" =~ smoke|test ]]; then
  echo "publish_full mode cannot use smoke/test config: ${CONFIG_PATH_VALUE}" >&2
  exit 1
fi

if [ "${RUN_GRID}" = "1" ]; then
  if prompt_yes_no "Publish winner if publishable?" "N"; then
    PUBLISH_WINNER="1"
  fi
fi

RUN_STAMP="$(date -u +%Y%m%d_%H%M%S)"
RUN_NONCE="$("${PYTHON_BIN}" - <<'PY'
import uuid
print(uuid.uuid4().hex[:8])
PY
)"
SAFE_GROUP="${MODEL_GROUP_VALUE//\//__}"
RUN_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/training_launches/${RUN_STAMP}_${RUN_NONCE}_${MODE_LABEL}_${LANE_TAG_VALUE}_${SAFE_GROUP}_${PROFILE_ID_VALUE}"
mkdir -p "${RUN_ROOT}"
LOG_PATH="${RUN_ROOT}/training.log"
TRAINING_RELEASE_JSON_PATH="${RUN_ROOT}/training-release.json"

echo
echo "Run plan:"
echo "  mode: ${MODE_ID}"
echo "  lane_tag: ${LANE_TAG_VALUE}"
echo "  base_model_group: ${MODEL_GROUP_BASE_VALUE}"
echo "  model_group: ${MODEL_GROUP_VALUE}"
echo "  profile_id: ${PROFILE_ID_VALUE}"
echo "  config: ${CONFIG_PATH_VALUE}"
echo "  launch_root: ${RUN_ROOT}"
echo "  log: ${LOG_PATH}"
if [ "${RUN_GRID}" = "0" ]; then
  echo "  release_json: ${TRAINING_RELEASE_JSON_PATH}"
  echo "  apply_runtime_handoff: ${APPLY_RUNTIME_HANDOFF_VALUE}"
  echo "  publish_runtime_config: ${PUBLISH_RUNTIME_CONFIG_VALUE}"
fi
echo

prompt_yes_no "Start training now?" "Y" || exit 0

if [ "${RUN_GRID}" = "1" ]; then
  cmd=(
    python -m ml_pipeline_2.run_staged_grid
    --config "${CONFIG_PATH_VALUE}"
    --model-group "${MODEL_GROUP_VALUE}"
    --profile-id "${PROFILE_ID_VALUE}"
  )
  if [ "${PUBLISH_WINNER}" = "1" ]; then
    cmd+=(--publish-winner --model-bucket-url "${MODEL_BUCKET_URL}")
  fi
  "${cmd[@]}" 2>&1 | tee "${LOG_PATH}"
  echo
  echo "Grid run complete. Log: ${LOG_PATH}"
  exit 0
fi

APPLY_RUNTIME_HANDOFF="${APPLY_RUNTIME_HANDOFF_VALUE}" \
PUBLISH_RUNTIME_CONFIG="${PUBLISH_RUNTIME_CONFIG_VALUE}" \
MODEL_GROUP="${MODEL_GROUP_VALUE}" \
PROFILE_ID="${PROFILE_ID_VALUE}" \
STAGED_CONFIG="${CONFIG_PATH_VALUE}" \
TRAINING_RELEASE_JSON="${TRAINING_RELEASE_JSON_PATH}" \
bash "${REPO_ROOT}/ops/gcp/run_staged_release_pipeline.sh" 2>&1 | tee "${LOG_PATH}"

"${PYTHON_BIN}" - <<'PY' "${TRAINING_RELEASE_JSON_PATH}" "${MODEL_BUCKET_URL}" "${MODEL_GROUP_VALUE}" "${PROFILE_ID_VALUE}"
import json
import sys
from pathlib import Path

release_path = Path(sys.argv[1])
model_bucket_url = str(sys.argv[2]).rstrip("/")
model_group = str(sys.argv[3]).strip("/")
profile_id = str(sys.argv[4]).strip()

if not release_path.exists():
    raise SystemExit(f"release json not found: {release_path}")

payload = json.loads(release_path.read_text(encoding="utf-8"))
release_status = str(payload.get("release_status") or "")
publish_status = str(((payload.get("publish") or {}).get("publish_status") or ""))
run_id = str(payload.get("run_id") or "")
run_dir = str(payload.get("run_dir") or "")
blocking = list((payload.get("assessment") or {}).get("blocking_reasons") or [])

print("Training summary:")
print(f"  release_status: {release_status}")
print(f"  publish_status: {publish_status}")
print(f"  run_id: {run_id}")
print(f"  run_dir: {run_dir}")
print(f"  release_json: {release_path}")
if blocking:
    print(f"  blocking_reasons: {', '.join(str(item) for item in blocking)}")
else:
    print("  blocking_reasons: none")

print("Expected publish path pattern:")
print(f"  {model_bucket_url}/{model_group}/")
print(f"  {model_bucket_url}/{model_group}/model/model.joblib")
print(f"  {model_bucket_url}/{model_group}/config/profiles/{profile_id}/threshold_report.json")
print(f"  {model_bucket_url}/{model_group}/config/profiles/{profile_id}/training_report.json")
print(f"  {model_bucket_url}/{model_group}/model_contract.json")
PY

echo
echo "Training run complete. Log: ${LOG_PATH}"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"

ensure_file() {
  local path="$1"
  if [ ! -f "${path}" ]; then
    echo "Required file not found: ${path}" >&2
    exit 1
  fi
}

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Required command not found: ${name}" >&2
    exit 1
  fi
}

ensure_supported_host() {
  local kernel
  kernel="$(uname -s 2>/dev/null || printf 'unknown')"
  case "${kernel}" in
    Linux)
      return 0
      ;;
    MINGW*|MSYS*|CYGWIN*)
      cat >&2 <<'EOF'
run_snapshot_parquet_pipeline.sh must be run from a Linux host.

Supported operator hosts:
- Ubuntu
- Cloud Shell
- WSL

Windows/Git Bash is supported only for raw archive upload, for example:
  RAW_ARCHIVE_BUCKET_URL=gs://<snapshot-bucket>/banknifty_data ./ops/gcp/publish_raw_market_data.sh /path/to/banknifty_data

Then run the full parquet pipeline from Linux against the same GCS raw archive.
EOF
      exit 1
      ;;
  esac
}

available_gb() {
  local target="$1"
  local kb
  kb="$(df -Pk "${target}" | awk 'NR==2 {print $4}')"
  if [ -z "${kb}" ]; then
    echo "Unable to determine free disk space for ${target}" >&2
    exit 1
  fi
  printf '%s\n' "$((kb / 1024 / 1024))"
}

ensure_free_disk_gb() {
  local target="$1"
  local minimum_gb="$2"
  local free_gb
  free_gb="$(available_gb "${target}")"
  if [ "${free_gb}" -lt "${minimum_gb}" ]; then
    cat >&2 <<EOF
Not enough free disk space for snapshot/parquet build.

Path checked: ${target}
Free space: ${free_gb}G
Required minimum: ${minimum_gb}G

Use a large-disk Linux VM for this workflow.
Cloud Shell is suitable for orchestration, not full parquet builds.
EOF
    exit 1
  fi
}

cpu_count() {
  local cpu_count
  if command -v nproc >/dev/null 2>&1; then
    cpu_count="$(nproc)"
  else
    cpu_count="$(
      python3 - <<'PY'
import os
print(os.cpu_count() or 1)
PY
    )"
  fi
  if [ "${cpu_count}" -lt 1 ]; then
    cpu_count=1
  fi
  printf '%s\n' "${cpu_count}"
}

default_normalize_jobs() {
  local count
  count="$(cpu_count)"
  if [ "${count}" -le 4 ]; then
    printf '1\n'
    return
  fi
  count="$((count - 2))"
  if [ "${count}" -gt 12 ]; then
    count=12
  fi
  printf '%s\n' "${count}"
}

default_snapshot_jobs() {
  local count
  count="$(cpu_count)"
  if [ "${count}" -le 4 ]; then
    printf '2\n'
    return
  fi
  count="$((count - 2))"
  if [ "${count}" -gt 6 ]; then
    count=6
  fi
  if [ "${count}" -lt 2 ]; then
    count=2
  fi
  printf '%s\n' "${count}"
}

ensure_raw_archive_layout() {
  local raw_root="$1"
  local missing=0
  local entry
  for entry in banknifty_fut banknifty_options banknifty_spot; do
    if [ ! -e "${raw_root}/${entry}" ]; then
      echo "Local raw archive is missing required entry: ${raw_root}/${entry}" >&2
      missing=1
    fi
  done
  if [ ! -e "${raw_root}/VIX" ] && [ ! -e "${raw_root}/vix" ]; then
    echo "Local raw archive is missing required VIX entry: ${raw_root}/VIX or ${raw_root}/vix" >&2
    missing=1
  fi
  if [ "${missing}" != "0" ]; then
    exit 1
  fi
}

json_get() {
  local path="$1"
  local expr="$2"
  python - <<'PY' "${path}" "${expr}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload
for part in sys.argv[2].split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

verify_stage2_required_columns() {
  local stage2_root="$1"
  local required_csv="$2"
  python - <<'PY' "${stage2_root}" "${required_csv}"
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

root = Path(sys.argv[1])
required = [item.strip() for item in str(sys.argv[2]).split(",") if item.strip()]
if not required:
    print(json.dumps({"status": "skipped", "reason": "no_required_columns"}))
    raise SystemExit(0)
if not root.exists():
    print(f"stage2 dataset path not found: {root}", file=sys.stderr)
    raise SystemExit(1)

files = sorted(root.rglob("*.parquet"))
if not files:
    print(f"no parquet files found under stage2 dataset path: {root}", file=sys.stderr)
    raise SystemExit(1)

missing = []
for path in files:
    names = set(pq.ParquetFile(path).schema_arrow.names)
    absent = [name for name in required if name not in names]
    if absent:
        missing.append({"path": str(path), "missing_columns": absent})

if missing:
    print(
        json.dumps(
            {
                "status": "failed",
                "checked_files": len(files),
                "required_columns": required,
                "missing_files": missing[:10],
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    json.dumps(
        {
            "status": "ok",
            "checked_files": len(files),
            "required_columns": required,
        },
        indent=2,
    )
)
PY
}

run_coverage_audit() {
  local audit_path="$1"
  python - <<'PY' "${PARQUET_BASE}" "${audit_path}" "${YEAR}" "${MIN_DAY}" "${MAX_DAY}"
import json
import sys
from datetime import datetime
from pathlib import Path

from snapshot_app.historical.parquet_store import ParquetStore


def collapse_days(days, *, max_gap_days=4):
    if not days:
        return []
    parsed = [datetime.strptime(day, "%Y-%m-%d").date() for day in days]
    out = []
    start = parsed[0]
    end = parsed[0]
    count = 1
    for current in parsed[1:]:
        gap = (current - end).days
        if gap <= max_gap_days:
            end = current
            count += 1
            continue
        out.append({"start": start.isoformat(), "end": end.isoformat(), "days": count})
        start = current
        end = current
        count = 1
    out.append({"start": start.isoformat(), "end": end.isoformat(), "days": count})
    return out


parquet_base, audit_path, year_text, min_day, max_day = sys.argv[1:6]
resolved_min = min_day or None
resolved_max = max_day or None
if year_text:
    year = int(year_text)
    resolved_min = f"{year:04d}-01-01"
    resolved_max = f"{year:04d}-12-31"

store = ParquetStore(parquet_base, snapshots_dataset="snapshots")
futures_days = set(store.available_days(min_day=resolved_min, max_day=resolved_max))
option_days = set(store.all_days_with_options(min_day=resolved_min, max_day=resolved_max))
built_days = set(store.available_snapshot_days(min_day=resolved_min, max_day=resolved_max))

buildable_missing = sorted((futures_days & option_days) - built_days)
source_missing = sorted(futures_days - option_days)

payload = {
    "requested_year": year_text or None,
    "requested_min_day": resolved_min,
    "requested_max_day": resolved_max,
    "futures_days": {
        "count": len(futures_days),
        "min": min(futures_days) if futures_days else None,
        "max": max(futures_days) if futures_days else None,
    },
    "option_days": {
        "count": len(option_days),
        "min": min(option_days) if option_days else None,
        "max": max(option_days) if option_days else None,
    },
    "built_days": {
        "count": len(built_days),
        "min": min(built_days) if built_days else None,
        "max": max(built_days) if built_days else None,
    },
    "buildable_missing_count": len(buildable_missing),
    "source_missing_count": len(source_missing),
    "buildable_missing_first_20": buildable_missing[:20],
    "buildable_missing_last_20": buildable_missing[-20:],
    "source_missing_first_20": source_missing[:20],
    "source_missing_last_20": source_missing[-20:],
    "buildable_missing_ranges": collapse_days(buildable_missing),
    "source_missing_ranges": collapse_days(source_missing),
}

Path(audit_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(json.dumps(payload, indent=2))
PY
}

snapshot_runner_base_args() {
  SNAPSHOT_RUNNER_ARGS=(
    python -m snapshot_app.historical.snapshot_batch_runner
    --base "${PARQUET_BASE}"
    --normalize-jobs "${NORMALIZE_JOBS}"
    --snapshot-jobs "${SNAPSHOT_JOBS}"
    --slice-months "${SNAPSHOT_SLICE_MONTHS}"
    --slice-warmup-days "${SNAPSHOT_SLICE_WARMUP_DAYS}"
    --build-stage "${BUILD_STAGE}"
    --build-source "${BUILD_SOURCE}"
    --build-run-id "${BUILD_RUN_ID}"
    --validate-days "${VALIDATE_DAYS}"
    --manifest-out "${REPORT_ROOT}/build_manifest.json"
    --validation-report-out "${REPORT_ROOT}/validation_report.json"
    --window-manifest-out "${REPORT_ROOT}/window_manifest_latest.json"
    --window-min-trading-days "${WINDOW_MIN_TRADING_DAYS}"
    --window-max-gap-days "${WINDOW_MAX_GAP_DAYS}"
  )

  if [ -n "${MIN_DAY}" ]; then
    SNAPSHOT_RUNNER_ARGS+=(--min-day "${MIN_DAY}")
  fi

  if [ -n "${MAX_DAY}" ]; then
    SNAPSHOT_RUNNER_ARGS+=(--max-day "${MAX_DAY}")
  fi

  if [ "${VALIDATE_ML_FLAT_CONTRACT}" = "1" ]; then
    SNAPSHOT_RUNNER_ARGS+=(--validate-ml-flat-contract)
  fi

  if [ -n "${YEAR}" ]; then
    SNAPSHOT_RUNNER_ARGS+=(--year "${YEAR}")
  fi

  if [ "${NO_RESUME}" = "1" ]; then
    SNAPSHOT_RUNNER_ARGS+=(--no-resume)
  fi
}

ensure_file "${OPERATOR_ENV_FILE}"
ensure_supported_host

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
RAW_DATA_ROOT="${RAW_DATA_ROOT:-${REPO_ROOT}/.cache/banknifty_data}"
LOCAL_RAW_ARCHIVE_ROOT="${LOCAL_RAW_ARCHIVE_ROOT:-}"
SYNC_RAW_ARCHIVE_FROM_GCS="${SYNC_RAW_ARCHIVE_FROM_GCS:-1}"
PUBLISH_SNAPSHOT_PARQUET="${PUBLISH_SNAPSHOT_PARQUET:-1}"
PUBLISH_DERIVED_ML_FLAT="${PUBLISH_DERIVED_ML_FLAT:-1}"
PUBLISH_NORMALIZED_CACHE="${PUBLISH_NORMALIZED_CACHE:-0}"
PUBLISH_STAGE_VIEWS="${PUBLISH_STAGE_VIEWS:-1}"
PUBLISH_MARKET_BASE="${PUBLISH_MARKET_BASE:-1}"
CLEAN_PUBLISH_PREFIXES="${CLEAN_PUBLISH_PREFIXES:-1}"
VERIFY_PUBLISHED_PREFIXES="${VERIFY_PUBLISHED_PREFIXES:-1}"
ALLOW_PARTIAL_PUBLISH="${ALLOW_PARTIAL_PUBLISH:-0}"
BUILD_STAGE="${BUILD_STAGE:-all}"
VALIDATE_DAYS="${VALIDATE_DAYS:-5}"
VALIDATE_ML_FLAT_CONTRACT="${VALIDATE_ML_FLAT_CONTRACT:-1}"
WINDOW_MIN_TRADING_DAYS="${WINDOW_MIN_TRADING_DAYS:-150}"
WINDOW_MAX_GAP_DAYS="${WINDOW_MAX_GAP_DAYS:-7}"
BUILD_SOURCE="${BUILD_SOURCE:-historical}"
MIN_DAY="${MIN_DAY:-}"
MAX_DAY="${MAX_DAY:-}"
YEAR="${YEAR:-}"
NO_RESUME="${NO_RESUME:-0}"
VALIDATE_ONLY="${VALIDATE_ONLY:-0}"
NORMALIZE_ONLY="${NORMALIZE_ONLY:-0}"
SNAPSHOT_SLICE_MONTHS="${SNAPSHOT_SLICE_MONTHS:-6}"
SNAPSHOT_SLICE_WARMUP_DAYS="${SNAPSHOT_SLICE_WARMUP_DAYS:-90}"
AUTO_NORMALIZE_JOBS="$(default_normalize_jobs)"
AUTO_SNAPSHOT_JOBS="$(default_snapshot_jobs)"
NORMALIZE_JOBS="${NORMALIZE_JOBS:-${AUTO_NORMALIZE_JOBS}}"
SNAPSHOT_JOBS="${SNAPSHOT_JOBS:-${AUTO_SNAPSHOT_JOBS}}"
RUN_ID="${RUN_ID:-${BUILD_RUN_ID:-snapshot_parquet_$(date -u +%Y%m%dT%H%M%SZ)}}"
BUILD_RUN_ID="${BUILD_RUN_ID:-${RUN_ID}}"
REPORT_ROOT="${REPORT_ROOT:-${MANIFEST_ROOT:-${REPO_ROOT}/.run/snapshot_parquet/${BUILD_RUN_ID}}}"
AUDIT_PATH="${REPORT_ROOT}/coverage_audit.json"
RAW_ARCHIVE_BUCKET_URL="${RAW_ARCHIVE_BUCKET_URL:?set RAW_ARCHIVE_BUCKET_URL in operator.env}"
SNAPSHOT_PARQUET_BUCKET_URL="${SNAPSHOT_PARQUET_BUCKET_URL:?set SNAPSHOT_PARQUET_BUCKET_URL in operator.env}"
STAGE2_REQUIRED_COLUMNS="${STAGE2_REQUIRED_COLUMNS:-pcr_change_5m,pcr_change_15m,atm_oi_ratio,near_atm_oi_ratio,atm_ce_oi,atm_pe_oi}"
MIN_FREE_DISK_GB="${MIN_FREE_DISK_GB:-150}"

ensure_file "${REPO_ROOT}/ops/gcp/publish_snapshot_parquet.sh"
require_command python3
require_command gcloud
ensure_free_disk_gb "${REPO_ROOT}" "${MIN_FREE_DISK_GB}"

if [ -n "${LOCAL_RAW_ARCHIVE_ROOT}" ]; then
  if [ ! -d "${LOCAL_RAW_ARCHIVE_ROOT}" ]; then
    echo "Local raw archive directory not found: ${LOCAL_RAW_ARCHIVE_ROOT}" >&2
    exit 1
  fi
  ensure_raw_archive_layout "${LOCAL_RAW_ARCHIVE_ROOT}"
  if [ "${SYNC_RAW_ARCHIVE_FROM_GCS}" != "1" ]; then
    echo "LOCAL_RAW_ARCHIVE_ROOT is set; forcing SYNC_RAW_ARCHIVE_FROM_GCS=1 so the build uses the canonical GCS raw archive."
    SYNC_RAW_ARCHIVE_FROM_GCS=1
  fi
fi

mkdir -p "${REPORT_ROOT}"

echo "== Step 1: Upload local raw archive to GCS =="
if [ -n "${LOCAL_RAW_ARCHIVE_ROOT}" ]; then
  echo "Syncing ${LOCAL_RAW_ARCHIVE_ROOT} -> ${RAW_ARCHIVE_BUCKET_URL%/}"
  gcloud storage rsync "${LOCAL_RAW_ARCHIVE_ROOT}" "${RAW_ARCHIVE_BUCKET_URL%/}" --recursive
else
  echo "LOCAL_RAW_ARCHIVE_ROOT not set; skipping local raw archive upload."
fi

echo
echo "== Step 2: Sync raw archive to local cache =="
if [ "${SYNC_RAW_ARCHIVE_FROM_GCS}" = "1" ]; then
  mkdir -p "${RAW_DATA_ROOT}"
  echo "Syncing ${RAW_ARCHIVE_BUCKET_URL%/} -> ${RAW_DATA_ROOT}"
  gcloud storage rsync "${RAW_ARCHIVE_BUCKET_URL%/}" "${RAW_DATA_ROOT}" --recursive
elif [ ! -d "${RAW_DATA_ROOT}" ]; then
  echo "RAW_DATA_ROOT does not exist and SYNC_RAW_ARCHIVE_FROM_GCS=0: ${RAW_DATA_ROOT}" >&2
  exit 1
else
  echo "SYNC_RAW_ARCHIVE_FROM_GCS=0; using existing local raw cache at ${RAW_DATA_ROOT}"
fi

echo
echo "== Step 3: Prepare Python environment =="
if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
REQUIREMENTS_FILE="${REPO_ROOT}/snapshot_app/requirements.txt"
REQ_HASH_FILE="${VENV_DIR}/.snapshot_requirements.sha256"
CURRENT_REQ_HASH="$(
  python - <<'PY' "${REQUIREMENTS_FILE}"
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
INSTALLED_REQ_HASH="$(cat "${REQ_HASH_FILE}" 2>/dev/null || true)"
if [ "${CURRENT_REQ_HASH}" != "${INSTALLED_REQ_HASH}" ]; then
  python -m pip install --upgrade pip
  python -m pip install -r "${REPO_ROOT}/snapshot_app/requirements.txt"
  printf '%s' "${CURRENT_REQ_HASH}" > "${REQ_HASH_FILE}"
else
  echo "snapshot_app requirements unchanged; skipping pip install"
fi

echo
echo "== Step 4: Normalize raw archive into parquet cache =="
NORMALIZE_CMD=(
  python -m snapshot_app.historical.snapshot_batch_runner
  --base "${PARQUET_BASE}"
  --raw-root "${RAW_DATA_ROOT}"
  --normalize-jobs "${NORMALIZE_JOBS}"
  --normalize-only
)
(
  cd "${REPO_ROOT}"
  "${NORMALIZE_CMD[@]}"
)

if [ "${NORMALIZE_ONLY}" = "1" ]; then
  echo
  echo "Snapshot parquet pipeline complete."
  echo "  build run id: ${BUILD_RUN_ID}"
  echo "  parquet base: ${PARQUET_BASE}"
  echo "  report root: ${REPORT_ROOT}"
  echo "  raw source gcs: ${RAW_ARCHIVE_BUCKET_URL}"
  echo "  raw cache: ${RAW_DATA_ROOT}"
  echo "  normalize jobs: ${NORMALIZE_JOBS}"
  exit 0
fi

echo
echo "== Step 5: Audit source coverage =="
run_coverage_audit "${AUDIT_PATH}"

FUTURES_COUNT="$(json_get "${AUDIT_PATH}" "futures_days.count")"
BUILDABLE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "buildable_missing_count")"
SOURCE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "source_missing_count")"

if [ "${FUTURES_COUNT}" = "0" ]; then
  echo "No futures days found in the requested window. Nothing to build or publish." >&2
  exit 1
fi

if [ "${SOURCE_MISSING_COUNT}" != "0" ]; then
  echo
  echo "Source coverage is still missing for ${SOURCE_MISSING_COUNT} day(s)." >&2
  echo "The raw or normalized options archive does not cover the full requested window." >&2
  echo "Update the raw archive under ${RAW_ARCHIVE_BUCKET_URL%/} and rerun the script." >&2
  exit 2
fi

echo
echo "== Step 6: Build snapshots and generate reports =="
echo "  build stage: ${BUILD_STAGE}"
if [ "${VALIDATE_ML_FLAT_CONTRACT}" = "1" ]; then
  echo "  derived SnapshotMLFlat contract validation: enabled"
else
  echo "  derived SnapshotMLFlat contract validation: disabled (operator override)"
fi
snapshot_runner_base_args
if [ "${VALIDATE_ONLY}" = "1" ]; then
  SNAPSHOT_RUNNER_ARGS+=(--validate-only)
fi
(
  cd "${REPO_ROOT}"
  "${SNAPSHOT_RUNNER_ARGS[@]}"
)

echo
echo "== Step 7: Re-audit build coverage =="
run_coverage_audit "${AUDIT_PATH}"

BUILDABLE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "buildable_missing_count")"
SOURCE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "source_missing_count")"

if [ "${SOURCE_MISSING_COUNT}" != "0" ] || [ "${BUILDABLE_MISSING_COUNT}" != "0" ]; then
  echo "Build did not close the requested window. Refusing to publish." >&2
  exit 3
fi

if [ "${BUILD_STAGE}" != "snapshots" ]; then
  echo
  echo "== Step 7b: Verify local Stage 2 schema =="
  verify_stage2_required_columns "${PARQUET_BASE}/stage2_direction_view" "${STAGE2_REQUIRED_COLUMNS}"
fi

if [ "${VALIDATE_ONLY}" = "1" ]; then
  echo
  echo "Snapshot parquet pipeline complete."
  echo "  build run id: ${BUILD_RUN_ID}"
  echo "  parquet base: ${PARQUET_BASE}"
  echo "  report root: ${REPORT_ROOT}"
  echo "  raw source gcs: ${RAW_ARCHIVE_BUCKET_URL}"
  echo "  raw cache: ${RAW_DATA_ROOT}"
  echo "  normalize jobs: ${NORMALIZE_JOBS}"
  echo "  snapshot jobs: ${SNAPSHOT_JOBS}"
  echo "  publish status: skipped (VALIDATE_ONLY=1)"
  exit 0
fi

CAN_PUBLISH=1
if [ "${ALLOW_PARTIAL_PUBLISH}" != "1" ]; then
  if [ ! -f "${REPORT_ROOT}/build_manifest.json" ]; then
    echo "Refusing to publish because build_manifest.json is missing." >&2
    exit 4
  fi
  PUBLISH_READY="$(
    python - <<'PY' "${REPORT_ROOT}/build_manifest.json"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
result = payload.get("result") or {}
status = str(result.get("status") or "").strip()
error_count = int(result.get("error_count") or 0)
skipped_missing_inputs = int(result.get("days_skipped_missing_inputs") or 0)
days_no_rows = int(result.get("days_no_rows") or 0)
print("1" if status in {"complete", "already_complete"} and error_count == 0 and skipped_missing_inputs == 0 and days_no_rows == 0 else "0")
PY
  )"
  if [ "${PUBLISH_READY}" != "1" ]; then
    CAN_PUBLISH=0
  fi
fi

if [ "${PUBLISH_SNAPSHOT_PARQUET}" != "1" ]; then
  CAN_PUBLISH=0
fi

if [ "${CAN_PUBLISH}" != "1" ]; then
  if [ "${PUBLISH_SNAPSHOT_PARQUET}" = "1" ]; then
    echo "Refusing to publish because the build manifest is not publishable. Set ALLOW_PARTIAL_PUBLISH=1 to override." >&2
    exit 5
  fi
  echo
  echo "Snapshot parquet pipeline complete."
  echo "  build run id: ${BUILD_RUN_ID}"
  echo "  parquet base: ${PARQUET_BASE}"
  echo "  report root: ${REPORT_ROOT}"
  echo "  raw source gcs: ${RAW_ARCHIVE_BUCKET_URL}"
  echo "  raw cache: ${RAW_DATA_ROOT}"
  echo "  normalize jobs: ${NORMALIZE_JOBS}"
  echo "  snapshot jobs: ${SNAPSHOT_JOBS}"
  echo "  publish status: skipped (PUBLISH_SNAPSHOT_PARQUET=0)"
  exit 0
fi

echo
echo "== Step 8: Clean remote publish prefixes =="
if [ "${CLEAN_PUBLISH_PREFIXES}" = "1" ]; then
  for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view reports; do
    echo "Cleaning remote prefix ${SNAPSHOT_PARQUET_BUCKET_URL%/}/${ds}"
    gcloud storage rm --recursive "${SNAPSHOT_PARQUET_BUCKET_URL%/}/${ds}" || true
  done
else
  echo "CLEAN_PUBLISH_PREFIXES=0; leaving existing remote objects in place."
fi

echo
echo "== Step 9: Publish parquet outputs =="
export REPO_ROOT PARQUET_BASE REPORT_ROOT SNAPSHOT_PARQUET_BUCKET_URL
export PUBLISH_DERIVED_ML_FLAT PUBLISH_NORMALIZED_CACHE PUBLISH_STAGE_VIEWS PUBLISH_MARKET_BASE
"${REPO_ROOT}/ops/gcp/publish_snapshot_parquet.sh"

if [ "${VERIFY_PUBLISHED_PREFIXES}" = "1" ]; then
  echo
  echo "== Step 10: Verify published GCS layout =="
  gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL%/}/reports/**"
  for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
    echo "== ${ds}"
    gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL%/}/${ds}/**" | grep 'data.parquet$' | sort
  done
fi

echo
echo "Snapshot parquet pipeline complete."
echo "  build run id: ${BUILD_RUN_ID}"
echo "  parquet base: ${PARQUET_BASE}"
echo "  report root: ${REPORT_ROOT}"
echo "  raw source gcs: ${RAW_ARCHIVE_BUCKET_URL}"
echo "  raw cache: ${RAW_DATA_ROOT}"
echo "  normalize jobs: ${NORMALIZE_JOBS}"
echo "  snapshot jobs: ${SNAPSHOT_JOBS}"
echo "  publish root: ${SNAPSHOT_PARQUET_BUCKET_URL}"

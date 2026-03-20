#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
RAW_DATA_ROOT="${RAW_DATA_ROOT:-${REPO_ROOT}/.cache/banknifty_data}"

SYNC_RAW_ARCHIVE_FROM_GCS="${SYNC_RAW_ARCHIVE_FROM_GCS:-1}"
CLEAN_PUBLISH_PREFIXES="${CLEAN_PUBLISH_PREFIXES:-0}"
VERIFY_PUBLISHED_PREFIXES="${VERIFY_PUBLISHED_PREFIXES:-1}"

NORMALIZE_JOBS="${NORMALIZE_JOBS:-8}"
SNAPSHOT_JOBS="${SNAPSHOT_JOBS:-8}"
SNAPSHOT_SLICE_MONTHS="${SNAPSHOT_SLICE_MONTHS:-6}"
SNAPSHOT_SLICE_WARMUP_DAYS="${SNAPSHOT_SLICE_WARMUP_DAYS:-90}"
VALIDATE_DAYS="${VALIDATE_DAYS:-5}"
WINDOW_MIN_TRADING_DAYS="${WINDOW_MIN_TRADING_DAYS:-150}"
WINDOW_MAX_GAP_DAYS="${WINDOW_MAX_GAP_DAYS:-7}"

BUILD_STAGE="${BUILD_STAGE:-all}"
BUILD_SOURCE="${BUILD_SOURCE:-historical}"
YEAR="${YEAR:-}"
MIN_DAY="${MIN_DAY:-}"
MAX_DAY="${MAX_DAY:-}"

RUN_ID="${RUN_ID:-snapshot_backfill_$(date -u +%Y%m%dT%H%M%SZ)}"
REPORT_ROOT="${REPORT_ROOT:-${REPO_ROOT}/.run/snapshot_parquet/${RUN_ID}}"
AUDIT_PATH="${REPORT_ROOT}/coverage_audit.json"

ensure_file() {
  local path="$1"
  if [ ! -f "${path}" ]; then
    echo "Required file not found: ${path}" >&2
    exit 1
  fi
}

json_get() {
  local path="$1"
  local expr="$2"
  python - <<'PY' "${path}" "${expr}"
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
value = payload
for part in sys.argv[2].split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

ensure_file "${OPERATOR_ENV_FILE}"
ensure_file "${REPO_ROOT}/ops/gcp/run_snapshot_parquet_pipeline.sh"
ensure_file "${REPO_ROOT}/ops/gcp/publish_snapshot_parquet.sh"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is required on this machine." >&2
  exit 1
fi

set -a
source "${OPERATOR_ENV_FILE}"
set +a

mkdir -p "${REPORT_ROOT}"

echo "== Step 1: Normalize and sync raw archive =="
export REPO_ROOT OPERATOR_ENV_FILE VENV_DIR PARQUET_BASE RAW_DATA_ROOT
export SYNC_RAW_ARCHIVE_FROM_GCS NORMALIZE_JOBS SNAPSHOT_JOBS SNAPSHOT_SLICE_MONTHS SNAPSHOT_SLICE_WARMUP_DAYS
export VALIDATE_DAYS WINDOW_MIN_TRADING_DAYS WINDOW_MAX_GAP_DAYS BUILD_STAGE BUILD_SOURCE YEAR MIN_DAY MAX_DAY
export PUBLISH_SNAPSHOT_PARQUET=0
export NORMALIZE_ONLY=1
export VALIDATE_ONLY=0
export BUILD_RUN_ID="${RUN_ID}"
export MANIFEST_ROOT="${REPORT_ROOT}"
"${REPO_ROOT}/ops/gcp/run_snapshot_parquet_pipeline.sh"
unset NORMALIZE_ONLY

if [ ! -d "${VENV_DIR}" ]; then
  echo "Virtualenv not found after normalization: ${VENV_DIR}" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo
echo "== Step 2: Audit source coverage =="
python - <<'PY' "${PARQUET_BASE}" "${AUDIT_PATH}" "${YEAR}" "${MIN_DAY}" "${MAX_DAY}"
import json
import sys
from datetime import datetime

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

with open(audit_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)

print(json.dumps(payload, indent=2))
PY

FUTURES_COUNT="$(json_get "${AUDIT_PATH}" "futures_days.count")"
BUILDABLE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "buildable_missing_count")"
SOURCE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "source_missing_count")"

if [ "${FUTURES_COUNT}" = "0" ]; then
  echo "No futures days found in the requested window. Nothing to do." >&2
  exit 1
fi

if [ "${SOURCE_MISSING_COUNT}" != "0" ]; then
  echo
  echo "Source coverage is still missing for ${SOURCE_MISSING_COUNT} day(s)." >&2
  echo "The raw/normalized options archive does not cover the full requested window." >&2
  echo "Upload the missing raw options archive to RAW_ARCHIVE_BUCKET_URL, then rerun this script." >&2
  exit 2
fi

echo
if [ "${BUILDABLE_MISSING_COUNT}" != "0" ]; then
  echo "== Step 3: Backfill newly buildable days =="
  export PUBLISH_SNAPSHOT_PARQUET=0
  export NORMALIZE_ONLY=0
  export VALIDATE_ONLY=0
  export BUILD_RUN_ID="${RUN_ID}"
  export MANIFEST_ROOT="${REPORT_ROOT}"
  "${REPO_ROOT}/ops/gcp/run_snapshot_parquet_pipeline.sh"

  echo
  echo "== Step 4: Re-audit after backfill =="
  python - <<'PY' "${PARQUET_BASE}" "${AUDIT_PATH}" "${YEAR}" "${MIN_DAY}" "${MAX_DAY}"
import json
import sys
from datetime import datetime

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

with open(audit_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)

print(json.dumps(payload, indent=2))
PY

  BUILDABLE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "buildable_missing_count")"
  SOURCE_MISSING_COUNT="$(json_get "${AUDIT_PATH}" "source_missing_count")"

  if [ "${SOURCE_MISSING_COUNT}" != "0" ] || [ "${BUILDABLE_MISSING_COUNT}" != "0" ]; then
    echo "Backfill did not close the requested window. Refusing to publish." >&2
    exit 3
  fi
else
  echo "== Step 3: Local parquet already complete for the requested window =="
  export BUILD_RUN_ID="${RUN_ID}"
  PUBLISH_ONLY_CMD=(
    python -m snapshot_app.historical.snapshot_batch_runner
    --base "${PARQUET_BASE}"
    --build-stage "${BUILD_STAGE}"
    --build-source "${BUILD_SOURCE}"
    --validate-ml-flat-contract
    --validate-days "${VALIDATE_DAYS}"
    --manifest-out "${REPORT_ROOT}/build_manifest.json"
    --validation-report-out "${REPORT_ROOT}/validation_report.json"
    --window-manifest-out "${REPORT_ROOT}/window_manifest_latest.json"
  )
  if [ -n "${YEAR}" ]; then
    PUBLISH_ONLY_CMD+=(--year "${YEAR}")
  fi
  if [ -n "${MIN_DAY}" ]; then
    PUBLISH_ONLY_CMD+=(--min-day "${MIN_DAY}")
  fi
  if [ -n "${MAX_DAY}" ]; then
    PUBLISH_ONLY_CMD+=(--max-day "${MAX_DAY}")
  fi
  "${PUBLISH_ONLY_CMD[@]}"
fi

echo
echo "== Step 5: Publish =="
if [ "${CLEAN_PUBLISH_PREFIXES}" = "1" ]; then
  for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
    echo "Cleaning remote prefix ${SNAPSHOT_PARQUET_BUCKET_URL%/}/${ds}"
    gcloud storage rm --recursive "${SNAPSHOT_PARQUET_BUCKET_URL%/}/${ds}" || true
  done
fi

export REPORT_ROOT
"${REPO_ROOT}/ops/gcp/publish_snapshot_parquet.sh"

if [ "${VERIFY_PUBLISHED_PREFIXES}" = "1" ]; then
  echo
  echo "== Step 6: Verify published dataset files =="
  for ds in snapshots market_base snapshots_ml_flat stage1_entry_view stage2_direction_view stage3_recipe_view; do
    echo "== ${ds}"
    gcloud storage ls "${SNAPSHOT_PARQUET_BUCKET_URL%/}/${ds}/**" | grep 'data.parquet$' | sort
  done
fi

echo
echo "Backfill and publish complete."
echo "  run id: ${RUN_ID}"
echo "  report root: ${REPORT_ROOT}"
echo "  parquet base: ${PARQUET_BASE}"
echo "  publish root: ${SNAPSHOT_PARQUET_BUCKET_URL}"

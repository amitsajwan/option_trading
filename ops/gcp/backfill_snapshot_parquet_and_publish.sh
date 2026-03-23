#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"

echo "ops/gcp/backfill_snapshot_parquet_and_publish.sh is deprecated; forwarding to ops/gcp/run_snapshot_parquet_pipeline.sh" >&2
exec "${REPO_ROOT}/ops/gcp/run_snapshot_parquet_pipeline.sh" "$@"

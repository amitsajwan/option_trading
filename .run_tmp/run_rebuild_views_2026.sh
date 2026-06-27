#!/bin/bash
set -e
cd /home/amits/bmm_run
echo "=== Rebuilding stage1_entry_view_v2 for 2026-06 ==="
python3 -m snapshot_app.historical.rebuild_stage_views_from_flat \
    --parquet-root /home/amits/parquet_data \
    --start-date 2026-06-01 \
    --end-date 2026-06-17 \
    --build-source "mongo_export_jun2026" \
    --build-run-id "jun2026_fwd_check" 2>&1
echo "=== Rebuild done ==="

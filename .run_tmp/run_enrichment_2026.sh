#!/bin/bash
set -e
cd /home/amits/bmm_run
echo "=== Running enrichment_runner for 2026-06 ==="
python3 -m snapshot_app.historical.enrichment_runner \
    --parquet-root /home/amits/parquet_data \
    --start-date 2026-06-01 \
    --end-date 2026-06-17 \
    --output-dataset snapshots_ml_flat_v2 \
    --workers 1 \
    --log-level INFO 2>&1
echo "=== Enrichment done ==="

#!/bin/bash
set -e
cd /home/amits/bmm_run
echo "=== Phase B: June-2026 Forward Check ==="
ARTIFACTS=/home/amits/bmm_run/ml_pipeline_2/artifacts/research

BMM_PROD=$(find "$ARTIFACTS" -name "model.joblib" -path "*bmm_prod_5m020*" | head -1)
VEL_BASE=$(find "$ARTIFACTS" -name "model.joblib" -path "*ab_5m020_base*" | head -1)
VEL_BMM=$(find  "$ARTIFACTS" -name "model.joblib" -path "*ab_5m020_bmm*"  | head -1)

echo "bmm_prod:     $BMM_PROD"
echo "velocity_base: $VEL_BASE"
echo "velocity_bmm:  $VEL_BMM"

python3 /home/amits/score_entry_models.py \
    --view-root    /home/amits/parquet_data/stage1_entry_view_v2 \
    --support-root /home/amits/parquet_data/snapshots_ml_flat_v2 \
    --start 2026-06-01 \
    --end   2026-06-17 \
    --bundle "bmm_prod=${BMM_PROD}" \
    --bundle "velocity_base=${VEL_BASE}" \
    --bundle "velocity_bmm=${VEL_BMM}" 2>&1

echo "=== Scoring done ==="

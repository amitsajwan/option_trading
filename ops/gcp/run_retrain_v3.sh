#!/bin/bash
# Retrain v3 entry + direction models for BankNifty and NIFTY.
# Run on ML VM. Requires Dhan data already fetched by dhan_data_pipeline.
#
# Usage:
#   bash ops/gcp/run_retrain_v3.sh [banknifty|nifty|all]
#
# Prerequisite: Dhan pipeline data must exist.
# BankNifty monthly regime (Nov2024 NSE switched from weekly to monthly expiry):
#   python -m ml_pipeline_2.scripts.dhan_data_pipeline fetch \
#       --instrument BANKNIFTY --start 2024-11-01 --end 2026-06-30 \
#       --token $DHAN_ACCESS_TOKEN --client-id $DHAN_CLIENT_ID \
#       --out-dir .data/dhan_pipeline/raw/banknifty
#   python -m ml_pipeline_2.scripts.dhan_data_pipeline build \
#       --raw-dir .data/dhan_pipeline/raw/banknifty \
#       --out-dir .data/dhan_pipeline/indicators/banknifty
#
# NIFTY 5 years (weekly expiry throughout — consistent regime):
#   python -m ml_pipeline_2.scripts.dhan_data_pipeline fetch \
#       --instrument NIFTY --start 2021-06-01 --end 2026-06-30 \
#       --token $DHAN_ACCESS_TOKEN --client-id $DHAN_CLIENT_ID \
#       --out-dir .data/dhan_pipeline/raw/nifty
#   python -m ml_pipeline_2.scripts.dhan_data_pipeline build \
#       --raw-dir .data/dhan_pipeline/raw/nifty \
#       --out-dir .data/dhan_pipeline/indicators/nifty

set -e
cd /opt/option_trading

INSTRUMENT="${1:-all}"
DATA_DIR=".data/dhan_pipeline/indicators"
N_TRIALS=80

# BankNifty: monthly regime only (Nov2024 NSE switched from weekly to monthly)
BN_TRAIN_START="2024-11-01"
BN_TRAIN_END="2026-03-31"
BN_VALID_START="2026-04-01"
BN_VALID_END="2026-05-31"
BN_HOLD_START="2026-06-01"
BN_HOLD_END="2026-06-30"

# NIFTY: 5 years available (weekly expiry throughout — consistent regime)
NF_TRAIN_START="2021-06-01"
NF_TRAIN_END="2025-12-31"
NF_VALID_START="2026-01-01"
NF_VALID_END="2026-04-30"
NF_HOLD_START="2026-05-01"
NF_HOLD_END="2026-06-30"

# Default for backward compat
TRAIN_START="$BN_TRAIN_START"
TRAIN_END="$BN_TRAIN_END"
VALID_START="$BN_VALID_START"
VALID_END="$BN_VALID_END"
HOLD_START="$BN_HOLD_START"
HOLD_END="$BN_HOLD_END"

echo "======================================================="
echo "  Retrain v3 — instrument=$INSTRUMENT"
echo "  Data dir: $DATA_DIR"
echo "  Train: $TRAIN_START → $TRAIN_END"
echo "  Valid: $VALID_START → $VALID_END"
echo "  Holdout: $HOLD_START → $HOLD_END"
echo "  HPO trials: $N_TRIALS"
echo "======================================================="

run_banknifty_entry() {
    echo ""
    echo ">>> BankNifty ENTRY model v3"
    python -m ml_pipeline_2.scripts.train_entry_dhan_v3 \
        --instrument BANKNIFTY \
        --data-dir "$DATA_DIR" \
        --output models/dhan_entry_bundle_v3.joblib \
        --train-start $TRAIN_START --train-end $TRAIN_END \
        --valid-start $VALID_START --valid-end $VALID_END \
        --holdout-start $HOLD_START --holdout-end $HOLD_END \
        --n-trials $N_TRIALS
    echo "BN entry DONE: models/dhan_entry_bundle_v3.joblib"
}

run_nifty_entry() {
    echo ""
    echo ">>> NIFTY ENTRY model v3 (5-year dataset)"
    python -m ml_pipeline_2.scripts.train_entry_dhan_v3 \
        --instrument NIFTY \
        --data-dir "$DATA_DIR" \
        --output models/nifty_entry_bundle_v3.joblib \
        --train-start $NF_TRAIN_START --train-end $NF_TRAIN_END \
        --valid-start $NF_VALID_START --valid-end $NF_VALID_END \
        --holdout-start $NF_HOLD_START --holdout-end $NF_HOLD_END \
        --n-trials $N_TRIALS
    echo "NIFTY entry DONE: models/nifty_entry_bundle_v3.joblib"
}

run_banknifty_direction() {
    echo ""
    echo ">>> BankNifty DIRECTION model v3"
    python -m ml_pipeline_2.scripts.train_direction_dhan_v3 \
        --instrument BANKNIFTY \
        --data-dir "$DATA_DIR" \
        --output models/direction_dhan_bn_v3.joblib \
        --train-start $TRAIN_START --train-end $TRAIN_END \
        --valid-start $VALID_START --valid-end $VALID_END \
        --holdout-start $HOLD_START --holdout-end $HOLD_END \
        --n-trials $N_TRIALS
    echo "BN direction DONE: models/direction_dhan_bn_v3.joblib"
}

run_nifty_direction() {
    echo ""
    echo ">>> NIFTY DIRECTION model v3 (5-year dataset)"
    python -m ml_pipeline_2.scripts.train_direction_dhan_v3 \
        --instrument NIFTY \
        --data-dir "$DATA_DIR" \
        --output models/direction_dhan_nifty_v3.joblib \
        --train-start $NF_TRAIN_START --train-end $NF_TRAIN_END \
        --valid-start $NF_VALID_START --valid-end $NF_VALID_END \
        --holdout-start $NF_HOLD_START --holdout-end $NF_HOLD_END \
        --n-trials $N_TRIALS
    echo "NIFTY direction DONE: models/direction_dhan_nifty_v3.joblib"
}

case $INSTRUMENT in
    banknifty|BANKNIFTY)
        run_banknifty_entry
        run_banknifty_direction
        ;;
    nifty|NIFTY)
        run_nifty_entry
        run_nifty_direction
        ;;
    all|*)
        run_banknifty_entry
        run_nifty_entry
        run_banknifty_direction
        run_nifty_direction
        ;;
esac

echo ""
echo "======================================================="
echo "  All models trained. Files in models/:"
ls -la models/*v3*.joblib 2>/dev/null || echo "  (check paths above)"
echo ""
echo "  Next steps:"
echo "  1. Check .report.json files for AUC + ship gates"
echo "  2. If ALL_PASS=True, update .env.compose model paths to v3"
echo "  3. git add models/*v3*.joblib && git commit"
echo "  4. Rebuild + redeploy strategy_app containers"
echo "======================================================="

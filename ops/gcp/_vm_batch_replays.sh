#!/usr/bin/env bash
set -uo pipefail
REPO=/opt/option_trading
RUN_DIR="${REPO}/ml_pipeline_2/artifacts/research/direction_s2_only_hpo_v2_20260522_190956"
LOG=/tmp/vm_batch_replays.log
exec > >(tee -a "$LOG") 2>&1
echo "=== batch start $(date -Is) ==="

run_step() {
  echo "=== $1 ==="
  if ! "${@:2}"; then
    echo "WARN: step failed: $1"
  fi
}

run_step "E2-S6 in_sample" sudo bash "$REPO/ops/gcp/run_ops_replay_suite.sh" in_sample

run_step "E3-S1 pe_only" sudo bash "$REPO/ops/gcp/run_engine_direction_ab.sh" pe_only

run_step "E3-S2 export" sudo -E env RUN_DIR="$RUN_DIR" bash "$REPO/ops/gcp/run_engine_direction_ab.sh" export_direction

run_step "E3-S2 direction_ml" sudo bash "$REPO/ops/gcp/run_engine_direction_ab.sh" direction_ml

echo "=== done $(date -Is) ==="

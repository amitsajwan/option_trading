#!/usr/bin/env bash
# Run debit top-3 rules_pipeline smoke + monthly audit on ML VM.
set -euo pipefail

REPO="${REPO:-/opt/option_trading}"
cd "${REPO}"
PYTHON="${PYTHON:-${REPO}/.venv/bin/python3}"
if [[ ! -x "${PYTHON}" ]]; then
  echo "FATAL: venv python not found at ${PYTHON}" >&2
  exit 1
fi

STAMP="${STAMP:-$(date +%Y%m%d)}"
SMOKE_OUT="ml_pipeline_2/artifacts/rules_runs/debit_top3_smoke_${STAMP}"
MONTHLY_OUT="ml_pipeline_2/artifacts/rules_runs/debit_top3_monthly_${STAMP}"

echo "== Building monthly matrix =="
"${PYTHON}" ml_pipeline_2/scripts/rules_pipeline/build_debit_top3_monthly_matrix.py

echo "== Smoke: 2 windows x 5 rules =="
"${PYTHON}" -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_debit_top3_smoke.json \
  --output-root "${SMOKE_OUT}"

echo "== Monthly: 51 months x 3 rules =="
"${PYTHON}" -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_debit_top3_monthly.json \
  --output-root "${MONTHLY_OUT}"

echo "== Done =="
echo "Smoke leaderboard: ${SMOKE_OUT}/leaderboard.md"
echo "Monthly leaderboard: ${MONTHLY_OUT}/leaderboard.md"

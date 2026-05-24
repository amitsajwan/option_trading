#!/usr/bin/env bash
set -euo pipefail
REPO="${REPO:-/opt/option_trading}"
cd "${REPO}"
PYTHON="${PYTHON:-${REPO}/.venv/bin/python3}"
STAMP="${STAMP:-$(date +%Y%m%d)}"
OUT="ml_pipeline_2/artifacts/rules_runs/playbook_v1_smoke_${STAMP}"
mkdir -p "$(dirname "${OUT}")"
"${PYTHON}" -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_playbook_v1_smoke.json \
  --output-root "${OUT}"
echo "Leaderboard: ${OUT}/leaderboard.md"

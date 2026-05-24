#!/usr/bin/env bash
# PASS month counts per rule from rules_pipeline leaderboard.md
set -euo pipefail
LB="${1:?leaderboard.md path}"
echo "# PASS counts — $(basename "$(dirname "$LB")")"
echo
for rule in PBV1_TOP3_THESIS PBV1_TOP3_THESIS_TRAIL PBV1_TOP3_CALM_THESIS PBV1_TOP3_QUALITY_THESIS R1S_TOP3_S3_COMPOSITE; do
  n=$(grep -c "PASS.*${rule}" "${LB}" 2>/dev/null || echo 0)
  echo "${rule}: ${n}"
done

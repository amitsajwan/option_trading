#!/usr/bin/env bash
# promote_winner.sh — manual deployment of a model-selection winner.
#
# This script ONLY swaps the historical strategy_app to use a specified
# option_pnl bundle. It does NOT:
#   - touch the live strategy_app
#   - automatically pick a "winner" — you specify by recipe + threshold
#   - re-train anything
#
# It is intentionally a separate, manual step. The pipeline produces a
# leaderboard.json; a human reads it and decides which cell to promote.
#
# Usage:
#   bash promote_winner.sh <bundle_dir> [--threshold 0.55]
#
# Where <bundle_dir> is the directory containing metadata.json + model.joblib
# (e.g. /opt/option_trading/.data/ml_pipeline/option_pnl_published_models/option_pnl_atm_pe_15_20260517_135208).
#
# After running, the historical container will use ONLY this bundle. A clean
# replay is recommended (clean_state_before_replay.sh) before drawing
# conclusions.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <bundle_dir> [--threshold N]" >&2
  exit 64
fi

BUNDLE_DIR=$1
THRESHOLD=""
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold) THRESHOLD=$2; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 64;;
  esac
done

if [[ ! -d "$BUNDLE_DIR" ]] || [[ ! -f "$BUNDLE_DIR/metadata.json" ]]; then
  echo "ERROR: bundle_dir invalid (no metadata.json): $BUNDLE_DIR" >&2
  exit 65
fi

CONTAINER_BUNDLE_PATH=$(echo "$BUNDLE_DIR" | sed -E 's|^/opt/option_trading/|/app/|')

echo "=== promote winner ==="
echo "  host path:      $BUNDLE_DIR"
echo "  container path: $CONTAINER_BUNDLE_PATH"
[ -n "$THRESHOLD" ] && echo "  override thr:   $THRESHOLD"
echo

# Optionally write threshold into the bundle's metadata.json (keeps a backup)
if [[ -n "$THRESHOLD" ]]; then
  cp "$BUNDLE_DIR/metadata.json" "$BUNDLE_DIR/metadata.json.bak_$(date +%Y%m%d_%H%M%S)"
  python3 - "$BUNDLE_DIR/metadata.json" "$THRESHOLD" <<'PY'
import json, sys
p, t = sys.argv[1], float(sys.argv[2])
d = json.loads(open(p).read())
d["decision_threshold"] = t
open(p, "w").write(json.dumps(d, indent=2))
print(f"set decision_threshold={t} in {p}")
PY
fi

# Set env on the historical compose service via direct env file edit
COMPOSE=/opt/option_trading/docker-compose.yml
if grep -nE "^      OPTION_PNL_MODEL_BUNDLE:" "$COMPOSE" | tail -1 | grep -q '.'; then
  # Comment out existing default, append our single-bundle override
  echo "(note) leaving compose default alone; setting via runtime env"
fi

# Restart the historical container with the new bundle env
cd /opt/option_trading
OPTION_PNL_MODEL_BUNDLE="$CONTAINER_BUNDLE_PATH" \
  docker compose --env-file .env.compose --profile historical \
  up -d --no-build --force-recreate strategy_app_historical

# Wait for healthy
until docker ps --format "{{.Names}} {{.Status}}" | grep -q "strategy_app_historical-1 .*healthy"; do sleep 2; done

echo
echo "=== container env after recreate ==="
docker exec option_trading-strategy_app_historical-1 sh -c "env | grep OPTION_PNL"

echo
echo "=== bundle loaded (startup logs) ==="
docker logs --tail 50 option_trading-strategy_app_historical-1 2>&1 | grep -E "OPTION-P|bundle" | tail -10

echo
echo "NEXT: run clean_state_before_replay.sh then trigger a replay to validate the winner under runtime conditions."

#!/usr/bin/env bash
# THE deploy path. No scp, no docker cp, no hand-edited containers.
#
#   sudo bash ops/deploy.sh                    # deploy core live services
#   sudo bash ops/deploy.sh strategy_app       # deploy just one service
#
# Steps: git pull --ff-only -> image build -> force-recreate -> POST-VERIFY:
#   1. code hash inside each container == repo (catches stale images / docker-cp drift)
#   2. config contract (ops/check_config_contract.py) — catches wiring gaps
#   3. container health summary
# Any verify failure exits non-zero: the deploy is NOT done until verified.
#
# Born 2026-07-07 after: exec-bit lost in scp (morning outage), docker-cp
# reverts, VM repo drift, "Up (healthy)" containers running stale code.
set -uo pipefail
REPO=/opt/option_trading
BRANCH="${DEPLOY_BRANCH:-feat/dhan-feature-engine}"
CORE_SERVICES="ingestion_app ingestion_app_nifty ingestion_app_finnifty snapshot_app snapshot_app_nifty snapshot_app_finnifty strategy_app strategy_app_nifty strategy_app_finnifty execution_app execution_app_nifty execution_app_finnifty seller_app seller_app_nifty seller_app_finnifty"
SERVICES="${*:-$CORE_SERVICES}"
# Sellers live in an overlay file; include it always so `deploy.sh seller_app`
# and full deploys go through the same one path (2026-07-11: sellers were
# hand-deployed at go-live and drifted out of this script).
COMPOSE="docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml -f docker-compose.seller.yml"
log(){ echo "[deploy $(date -u +%H:%M:%SZ)] $*"; }
fail(){ log "FAILED: $*"; exit 1; }

cd "$REPO" || fail "repo missing"

log "1/5 git pull --ff-only origin $BRANCH"
git fetch origin "$BRANCH" || fail "git fetch"
git merge --ff-only "origin/$BRANCH" || fail "git pull is not fast-forward — resolve VM repo drift first (git status)"
log "at commit: $(git rev-parse --short HEAD) $(git log -1 --format=%s | head -c 60)"

log "2/5 building images: $SERVICES"
$COMPOSE build $SERVICES || fail "image build"

log "3/5 recreating containers"
$COMPOSE up -d --no-deps --force-recreate $SERVICES || fail "compose up"
sleep 20

log "4/5 verify: code in container == repo"
code_dir_for(){ # service -> the source dir baked into its image
  case "$1" in
    ingestion_app*) echo ingestion_app;;
    snapshot_app*)  echo snapshot_app;;
    strategy_app*)  echo strategy_app;;
    execution_app*) echo execution_app;;
    seller_app*)    echo strategy_app;;   # seller image bakes strategy_app (runner lives there)
    *)              echo "";;
  esac
}
for svc in $SERVICES; do
  dir=$(code_dir_for "$svc"); [ -n "$dir" ] || continue
  cname="option_trading-${svc}-1"
  repo_hash=$(cd "$REPO/$dir" && find . -name '*.py' -not -path '*/tests/*' | sort | xargs md5sum 2>/dev/null | md5sum | cut -d' ' -f1)
  cont_hash=$(docker exec "$cname" sh -c \
    "cd /app/$dir && find . -name '*.py' -not -path '*/tests/*' | sort | xargs md5sum 2>/dev/null | md5sum | cut -d' ' -f1" 2>/dev/null)
  if [ "$repo_hash" = "$cont_hash" ] && [ -n "$repo_hash" ]; then
    log "  ✓ $svc code matches repo ($repo_hash)"
  else
    fail "$svc code MISMATCH (repo=$repo_hash container=$cont_hash) — stale image?"
  fi
done

log "5/5 verify: config contract + health"
python3 "$REPO/ops/check_config_contract.py" --quiet || fail "config contract violation — see above"
unhealthy=$(docker ps --format '{{.Names}} {{.Status}}' | grep -c unhealthy || true)
if [ "$unhealthy" != "0" ]; then
  docker ps --format '{{.Names}} {{.Status}}' | grep unhealthy
  log "WARNING: $unhealthy unhealthy container(s) — check before market open"
fi
log "DEPLOY VERIFIED at $(git rev-parse --short HEAD)"

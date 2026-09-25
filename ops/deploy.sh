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
CORE_SERVICES="ingestion_app ingestion_app_nifty ingestion_app_finnifty ingestion_app_sensex ingestion_app_midcpnifty snapshot_app snapshot_app_nifty snapshot_app_finnifty snapshot_app_sensex snapshot_app_midcpnifty strategy_app strategy_app_nifty strategy_app_finnifty strategy_app_sensex strategy_app_midcpnifty execution_app execution_app_nifty execution_app_finnifty execution_app_sensex execution_app_midcpnifty seller_app seller_app_nifty seller_app_finnifty seller_app_sensex seller_app_midcpnifty"
SERVICES="${*:-$CORE_SERVICES}"
# Sellers live in an overlay file; include it always so `deploy.sh seller_app`
# and full deploys go through the same one path (2026-07-11: sellers were
# hand-deployed at go-live and drifted out of this script).
COMPOSE="docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml -f docker-compose.seller.yml"
log(){ echo "[deploy $(date -u +%H:%M:%SZ)] $*"; }
fail(){ log "FAILED: $*"; exit 1; }

cd "$REPO" || fail "repo missing"

log "1/5 git pull --ff-only origin $BRANCH"
BEFORE_REV=$(git rev-parse HEAD)
git fetch origin "$BRANCH" || fail "git fetch"
git merge --ff-only "origin/$BRANCH" || fail "git pull is not fast-forward — resolve VM repo drift first (git status)"
log "at commit: $(git rev-parse --short HEAD) $(git log -1 --format=%s | head -c 60)"

# 2026-09-25: the line above just git-pulled THIS FILE. A running bash
# process's behavior when its own script changes on disk mid-execution is
# undefined, and it bit a real deploy today: a pull that changed deploy.sh
# itself (adding the base-image build-set fix below) silently kept running
# the OLD build logic for the rest of that same invocation -- proven by
# re-running immediately after with nothing left to pull (a no-op pull),
# which then correctly showed the new logic. Re-exec into a fresh
# interpreter of whatever is now actually on disk so every step after this
# one is guaranteed current, never whatever bash had already buffered.
if [ "$(git rev-parse HEAD)" != "$BEFORE_REV" ] && [ -z "${DEPLOY_REEXECED:-}" ]; then
  export DEPLOY_REEXECED=1
  exec bash "$0" "$@"
fi

# 2026-09-25: every non-primary instrument's service (execution_app_nifty,
# strategy_app_sensex, seller_app_finnifty, ...) is declared with `image:`
# only, no `build:` of its own -- it reuses the image the bare/primary
# service builds (e.g. seller_app_sensex runs `image: option_trading-
# seller_app`, built only by the `seller_app` service block). `compose
# build <suffixed-service>` is therefore a silent no-op for it: no error,
# exits fast, and step 3 force-recreates the container on whatever image
# already existed -- which found a real stale-code deploy today when
# `deploy.sh seller_app_sensex seller_app_finnifty` was run deliberately
# WITHOUT the bare `seller_app` (to avoid touching BankNifty's halted
# real-money container), so nothing ever rebuilt the shared image.
# Fix: always build each requested service's OWN base/primary service too
# -- building an image never starts or recreates a container, so this is
# safe even when the base service (BankNifty/primary) must stay untouched;
# only $SERVICES (unchanged below) is ever passed to `up --force-recreate`.
base_family_for(){ # suffixed service -> the base service owning its image's build:
  case "$1" in
    *_nifty)      echo "${1%_nifty}";;
    *_finnifty)   echo "${1%_finnifty}";;
    *_sensex)     echo "${1%_sensex}";;
    *_midcpnifty) echo "${1%_midcpnifty}";;
    *)            echo "$1";;
  esac
}
BUILD_SERVICES=""
for svc in $SERVICES; do
  case " $BUILD_SERVICES " in *" $svc "*) ;; *) BUILD_SERVICES="$BUILD_SERVICES $svc";; esac
  base=$(base_family_for "$svc")
  if [ "$base" != "$svc" ]; then
    case " $BUILD_SERVICES " in *" $base "*) ;; *) BUILD_SERVICES="$BUILD_SERVICES $base";; esac
  fi
done

log "2/5 building images: $BUILD_SERVICES"
$COMPOSE build $BUILD_SERVICES || fail "image build"

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

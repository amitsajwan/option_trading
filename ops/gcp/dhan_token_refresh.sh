#!/usr/bin/env bash
# Daily Dhan token refresh via TOTP — runs at 02:30 UTC (08:00 IST) pre-market.
# Mints a fresh 24h token and recreates ALL Dhan-dependent containers so every
# service gets the new token. Previously only recreated ingestion_app + execution_app,
# leaving ingestion_app_nifty and execution_app_nifty with stale tokens → 401 errors
# throughout the trading day. Fix: recreate all 4.
set -euo pipefail
REPO=/opt/option_trading
log(){ echo "[$(date -u +%FT%TZ)] $*"; }

log "dhan TOTP token refresh starting"
python3 $REPO/ops/gcp/dhan_totp_refresh.py

log "token persisted; recreating ALL Dhan-dependent containers"
cd "$REPO"
docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --no-deps --force-recreate \
  ingestion_app \
  ingestion_app_nifty \
  execution_app \
  execution_app_nifty
log "dhan token refreshed via TOTP + all 4 Dhan containers recreated"

#!/bin/bash
set -u
DAYS="2026-04-10 2026-04-13 2026-04-15 2026-04-16 2026-04-17 2026-04-20 2026-04-21 2026-04-22 2026-04-23 2026-04-24 2026-04-27 2026-04-28 2026-04-29 2026-04-30 2026-05-04 2026-05-05 2026-05-06 2026-05-07 2026-05-08 2026-05-11 2026-05-12 2026-05-13 2026-05-14 2026-05-15 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22"
SETTLE_SEC=280
for d in $DAYS; do
  echo "=== $d PUBLISH START $(date -u +%H:%M:%S) ==="
  docker run --rm --network option_trading_default -v /tmp/pubmongo.py:/pub.py -e REDIS_HOST=redis -e REDIS_PORT=6379 option_trading-dashboard python /pub.py NIFTY "$d" 1800
  echo "=== $d PUBLISHED, settling ${SETTLE_SEC}s ==="
  sleep "$SETTLE_SEC"
done
echo ALL_DAYS_DONE_NIFTY_SCALPER29

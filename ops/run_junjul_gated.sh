#!/bin/bash
# Gated replay: publish one day, then sleep a FIXED generous delay (bounds
# backlog to <=1 day at a time) before publishing the next day. The earlier
# ungated 29-day run silently lost messages once backlog grew past what
# pub/sub (no persistence) could hold. Fixed delay (not log-pattern polling)
# because many bars legitimately produce zero log output (score < threshold),
# making "wait for a specific log line" unreliable.
set -u
DAYS="2026-06-01 2026-06-02 2026-06-03 2026-06-04 2026-06-05 2026-06-08 2026-06-09 2026-06-10 2026-06-11 2026-06-12 2026-06-15 2026-06-16 2026-06-17 2026-06-18 2026-06-19 2026-06-22 2026-06-23 2026-06-24 2026-06-25 2026-06-29 2026-06-30 2026-07-01 2026-07-02 2026-07-03 2026-07-06 2026-07-07 2026-07-08 2026-07-09 2026-07-10"
SETTLE_SEC=320   # observed single-day consume time (isolated) was ~210-250s; +30% margin

for d in $DAYS; do
  echo "=== $d PUBLISH START $(date -u +%H:%M:%S) ==="
  docker run --rm --network option_trading_default -v /tmp/pub.py:/pub.py \
    -e REDIS_HOST=redis -e REDIS_PORT=6379 -e INGESTION_APP_BANKNIFTY_URL=http://ingestion_app:8004 \
    option_trading-dashboard python /pub.py BANKNIFTY "$d" 1800
  echo "=== $d PUBLISHED, settling ${SETTLE_SEC}s before next day ==="
  sleep "$SETTLE_SEC"
done
echo ALL_DAYS_DONE_GATED

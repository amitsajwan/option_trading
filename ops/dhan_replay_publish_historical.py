#!/usr/bin/env python3
"""Publish one trading day's Dhan historical data through the REAL
LiveMarketSnapshotBuilder onto the isolated market:snapshot:v1:historical
topic (consumed only by strategy_app_historical -> strategy_persistence_
app_historical -> Mongo *_historical collections, never touching live).

Reuses the exact fetch/build logic from market_data_dashboard.routes.
replay_routes (the "Multi" page mechanism) — only the target topic differs
(historical, not live), so a second isolated engine config can consume it
without fighting the live consumer's single-consumer-lock.

Run inside a container with market_data_dashboard + snapshot_app + redis:
  docker run --rm --network option_trading_default \
    -v /path/dhan_replay_publish_historical.py:/pub.py \
    option_trading-dashboard python /pub.py BANKNIFTY 2026-06-02 300
"""
import json
import os
import sys
import time

instrument = sys.argv[1] if len(sys.argv) > 1 else "BANKNIFTY"
date = sys.argv[2] if len(sys.argv) > 2 else None
speed = float(sys.argv[3]) if len(sys.argv) > 3 else 1200.0  # bars/min

if not date:
    print("usage: dhan_replay_publish_historical.py INSTRUMENT YYYY-MM-DD [speed]")
    sys.exit(1)

from market_data_dashboard.services.dhan_replay_fetcher import DhanHistoricalFetcher
from market_data_dashboard.services.dhan_replay_ingestion_server import DhanReplayIngestionServer
from snapshot_app.core.market_snapshot import LiveMarketSnapshotBuilder
from contracts_app import build_snapshot_event
from datetime import datetime, timedelta
import redis as redis_lib

TOPIC = "market:snapshot:v1:historical"

def log(msg):
    print(f"[{date}] {msg}", flush=True)

fetcher = DhanHistoricalFetcher(progress_cb=lambda step, msg: log(f"{step}: {msg}"))
raw = fetcher.fetch_day(instrument, date)
index_bars = raw.get("index_bars") or []
if not index_bars:
    log("NO_BARS — holiday or fetch failure, skipping")
    sys.exit(0)
n_opts = sum(len(s.get("ce", [])) + len(s.get("pe", [])) for s in (raw.get("options") or {}).values())
log(f"fetched {len(index_bars)} index bars, {n_opts} option bars")

prev_day_bars = []
for offset in range(1, 6):
    pd = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
    f2 = DhanHistoricalFetcher()
    raw2 = f2.fetch_day(instrument, pd, strikes=1)
    if raw2.get("index_bars"):
        prev_day_bars = raw2["index_bars"]
        log(f"prev-day bars from {pd}: {len(prev_day_bars)}")
        break

expiry_date = None
try:
    import requests
    base = os.getenv("INGESTION_APP_NIFTY_URL" if instrument.upper() == "NIFTY" else "INGESTION_APP_BANKNIFTY_URL",
                     "http://ingestion_app_nifty:8004" if instrument.upper() == "NIFTY" else "http://ingestion_app:8004")
    resp = requests.get(f"{base}/api/v1/options/chain/{instrument.upper()}", timeout=10)
    if resp.ok:
        expiry_date = resp.json().get("expiry")
except Exception as e:
    log(f"expiry fetch failed: {e}")

prev_day_close = None
if prev_day_bars:
    try:
        v = prev_day_bars[-1].get("close")
        prev_day_close = float(v) if v is not None else None
    except (TypeError, ValueError):
        pass

def velocity_ctx(trade_date):
    if trade_date == date and prev_day_close is not None:
        return (prev_day_close, None, None)
    return (None, None, None)

r = redis_lib.Redis(host=os.getenv("REDIS_HOST", "redis"), port=int(os.getenv("REDIS_PORT", "6379")),
                    db=0, decode_responses=True)
interval = 60.0 / max(1.0, speed)
emitted = 0
last_snapshot_id = None

with DhanReplayIngestionServer(raw, prev_day_bars=prev_day_bars, expiry_date=expiry_date) as srv:
    builder = LiveMarketSnapshotBuilder(
        instrument=f"{instrument.upper()}FUT",
        market_api_base=srv.base_url,
        dashboard_api_base=srv.base_url,
        velocity_context_provider=velocity_ctx,
    )
    for i, _ in enumerate(index_bars):
        srv.set_bar(i)
        try:
            snapshot = builder.build_snapshot(ohlc_limit=800)
        except Exception as exc:
            continue
        sid = str(snapshot.get("snapshot_id") or "")
        if sid == last_snapshot_id:
            continue
        last_snapshot_id = sid
        event = build_snapshot_event(
            snapshot=snapshot, source="dhan_replay_historical",
            metadata={"run_id": f"replay-hist-{date}-{instrument.lower()}", "replay_date": date, "bar_index": i})
        r.publish(TOPIC, json.dumps(event, default=str))
        emitted += 1
        if emitted % 100 == 0:
            log(f"published {emitted}/{len(index_bars)}")
        if interval > 0.005:
            time.sleep(interval)

log(f"DONE emitted={emitted} topic={TOPIC}")

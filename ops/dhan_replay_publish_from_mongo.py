#!/usr/bin/env python3
"""Publish one trading day through the REAL LiveMarketSnapshotBuilder,
sourced from the already quality-gated phase1_market_snapshots_hist Mongo
collection instead of a live Dhan API fetch (user request 2026-07-13, after
today's replay-driven Dhan instability corrupted the adaptive-mode run —
14/29 days lost to fetch failures caused by heavy concurrent load on the
live account). Zero Dhan API calls; same fidelity as dhan_replay_publish_
historical.py otherwise (velocity_enrichment, EMA, IV percentile all
computed fresh by the real builder, not read from the stored snapshot).

Run inside a container with market_data_dashboard + snapshot_app + redis
+ pymongo (no live Dhan credentials needed at all):
  docker run --rm --network option_trading_default \
    -v /path/dhan_replay_publish_from_mongo.py:/pub.py \
    option_trading-dashboard python /pub.py BANKNIFTY 2026-06-03
"""
import json
import os
import sys
import time

instrument = sys.argv[1] if len(sys.argv) > 1 else "BANKNIFTY"
date = sys.argv[2] if len(sys.argv) > 2 else None
speed = float(sys.argv[3]) if len(sys.argv) > 3 else 1200.0

if not date:
    print("usage: dhan_replay_publish_from_mongo.py INSTRUMENT YYYY-MM-DD [speed]")
    sys.exit(1)

from pymongo import MongoClient
from market_data_dashboard.services.dhan_replay_ingestion_server import DhanReplayIngestionServer
from snapshot_app.core.market_snapshot import LiveMarketSnapshotBuilder
from contracts_app import build_snapshot_event
from datetime import datetime, timedelta
import redis as redis_lib

TOPIC = "market:snapshot:v1:historical"
STEP = 100 if instrument.upper() == "BANKNIFTY" else 50
COLL = "phase1_market_snapshots_hist" if instrument.upper() == "BANKNIFTY" else "phase1_market_snapshots_hist_nifty"


def log(msg):
    print(f"[{date}] {msg}", flush=True)


db = MongoClient("mongo", 27017).trading_ai
coll = db[COLL]


def _day_docs(d):
    return list(coll.find({"trade_date_ist": d}).sort("timestamp", 1))


def _build_raw(docs):
    """Reshape stored (already-built) snapshots back into the 'raw Dhan
    fetch' shape DhanReplayIngestionServer expects: index_bars (OHLC) +
    options{label:{ce:[...],pe:[...]}}, labels relative to each bar's own
    ATM (ATM, ATMp1, ATMm1, ...) since strikes in storage are absolute."""
    index_bars = []
    opt_ce = {}  # label -> list of {ts, ce_close, ce_iv, ce_oi, ce_volume}
    opt_pe = {}
    atm_strike = None
    for doc in docs:
        p = (doc.get("payload") or {}).get("snapshot") or {}
        fb = p.get("futures_bar") or {}
        ts = p.get("timestamp") or doc.get("timestamp") or ""
        index_bars.append({
            "ts": ts, "start_at": ts,
            "open": fb.get("open") or fb.get("fut_close"),
            "high": fb.get("high"), "low": fb.get("low"),
            "close": fb.get("fut_close"), "volume": fb.get("volume"),
        })
        atm = (p.get("chain_aggregates") or {}).get("atm_strike")
        if atm:
            atm_strike = atm  # last-known ATM (server uses this as fallback)
        for row in (p.get("strikes") or []):
            strike = row.get("strike")
            if strike is None or atm is None:
                continue
            offset = round((strike - atm) / STEP)
            label = "ATM" if offset == 0 else (f"ATMp{offset}" if offset > 0 else f"ATMm{-offset}")
            ts16 = str(ts)[:16]
            if row.get("ce_ltp") is not None:
                opt_ce.setdefault(label, []).append({
                    "ts": ts16, "ce_close": row.get("ce_ltp"), "ce_iv": row.get("ce_iv"),
                    "ce_oi": row.get("ce_oi"), "ce_volume": 0,
                })
            if row.get("pe_ltp") is not None:
                opt_pe.setdefault(label, []).append({
                    "ts": ts16, "pe_close": row.get("pe_ltp"), "pe_iv": row.get("pe_iv"),
                    "pe_oi": row.get("pe_oi"), "pe_volume": 0,
                })
    labels = set(opt_ce) | set(opt_pe)
    options = {lbl: {"ce": opt_ce.get(lbl, []), "pe": opt_pe.get(lbl, [])} for lbl in labels}
    vix_bars = [{"close": ((doc.get("payload") or {}).get("snapshot") or {}).get("vix_context", {}).get("vix_current")}
                for doc in docs]
    return {
        "index_bars": index_bars, "vix_bars": vix_bars, "options": options,
        "instrument": instrument.upper(), "step": STEP, "atm_strike": atm_strike,
    }


docs = _day_docs(date)
if not docs:
    log("NO_DOCS — day not in quality-gated collection, skipping")
    sys.exit(0)
raw = _build_raw(docs)
n_opts = sum(len(v["ce"]) + len(v["pe"]) for v in raw["options"].values())
log(f"reshaped {len(raw['index_bars'])} index bars, {n_opts} option bars from Mongo (no Dhan calls)")

prev_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
prev_docs = []
for offset in range(1, 6):
    pd = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
    prev_docs = _day_docs(pd)
    if prev_docs:
        break
prev_day_bars = []
for doc in prev_docs:
    p = (doc.get("payload") or {}).get("snapshot") or {}
    fb = p.get("futures_bar") or {}
    ts = p.get("timestamp") or doc.get("timestamp") or ""
    prev_day_bars.append({"ts": ts, "start_at": ts, "open": fb.get("open") or fb.get("fut_close"),
                          "high": fb.get("high"), "low": fb.get("low"),
                          "close": fb.get("fut_close"), "volume": fb.get("volume")})
prev_day_close = None
if prev_day_bars:
    try:
        prev_day_close = float(prev_day_bars[-1]["close"])
    except (TypeError, ValueError):
        pass
log(f"prev-day bars from {prev_date if prev_docs else '(none found)'}: {len(prev_day_bars)}")

expiry_date = None
first_p = (docs[0].get("payload") or {}).get("snapshot") or {}
sc = first_p.get("session_context") or {}
expiry_date = sc.get("expiry")

def velocity_ctx(trade_date):
    return (prev_day_close, None, None) if trade_date == date and prev_day_close is not None else (None, None, None)

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
    for i in range(len(raw["index_bars"])):
        srv.set_bar(i)
        try:
            snapshot = builder.build_snapshot(ohlc_limit=800)
        except Exception:
            continue
        sid = str(snapshot.get("snapshot_id") or "")
        if sid == last_snapshot_id:
            continue
        last_snapshot_id = sid
        event = build_snapshot_event(
            snapshot=snapshot, source="dhan_replay_from_mongo",
            metadata={"run_id": f"replay-mongo-{date}-{instrument.lower()}", "replay_date": date, "bar_index": i})
        r.publish(TOPIC, json.dumps(event, default=str))
        emitted += 1
        if emitted % 100 == 0:
            log(f"published {emitted}/{len(raw['index_bars'])}")
        if interval > 0.005:
            time.sleep(interval)

log(f"DONE emitted={emitted} topic={TOPIC}")

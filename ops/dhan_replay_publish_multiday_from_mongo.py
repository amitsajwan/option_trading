#!/usr/bin/env python3
"""Publish MULTIPLE trading days through the REAL LiveMarketSnapshotBuilder
in ONE CONTINUOUS PROCESS, keeping the same builder instance alive across
day boundaries.

Why this exists (2026-07-15): dhan_replay_publish_from_mongo.py processes
one day per `docker run` invocation. That's fine for most state (velocity
accumulators, EMA, etc. are day-scoped and reset correctly), but
builder.state.iv_history_expiry/non_expiry is a CROSS-DAY, in-memory-only
accumulator (see snapshot_app/core/market_snapshot.py MarketSnapshotState) —
live production keeps ONE process running for weeks so this deque genuinely
warms up. A fresh process per day means it starts empty every single day, so
iv_percentile trivially reads ~100 for most of any cold day (the current bar
IS the highest ever seen when "ever" means "this day so far") and IV_FILTER
blocks nearly everything before ML_ENTRY ever runs. Confirmed on a real
29-day NIFTY replay: 254/254 recorded votes were IV_FILTER SKIPs, 156 of
those at exactly percentile=100, including the very first bar of day 1.

This script keeps the SAME LiveMarketSnapshotBuilder (and therefore the same
in-memory state.iv_history_*) alive across ALL requested days, swapping the
underlying DhanReplayIngestionServer's data via set_day() between days
instead of tearing the process down. No Dhan API calls (same Mongo source as
the single-day version).

Usage (inside a container with market_data_dashboard + snapshot_app + redis
+ pymongo, on the compose network):
  docker run --rm --network option_trading_default \
    -v /path/dhan_replay_publish_multiday_from_mongo.py:/pub.py \
    option_trading-dashboard python /pub.py NIFTY \
    2026-04-10,2026-04-13,2026-04-15,... [speed]
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

instrument = sys.argv[1] if len(sys.argv) > 1 else "BANKNIFTY"
dates_arg = sys.argv[2] if len(sys.argv) > 2 else None
speed = float(sys.argv[3]) if len(sys.argv) > 3 else 1200.0

if not dates_arg:
    print("usage: dhan_replay_publish_multiday_from_mongo.py INSTRUMENT YYYY-MM-DD[,YYYY-MM-DD,...] [speed]")
    sys.exit(1)

dates = sorted(dates_arg.split(","))

from pymongo import MongoClient
from market_data_dashboard.services.dhan_replay_ingestion_server import DhanReplayIngestionServer
from snapshot_app.core.market_snapshot import LiveMarketSnapshotBuilder
from contracts_app import build_snapshot_event
from contracts_app.event_bus import RedisEventBus
from contracts_app.topics import stream_name_for_topic

TOPIC = "market:snapshot:v1:historical"
STEP = 100 if instrument.upper() == "BANKNIFTY" else 50
COLL = "phase1_market_snapshots_hist" if instrument.upper() == "BANKNIFTY" else "phase1_market_snapshots_hist_nifty"


def log(msg, date=""):
    print(f"[{date}] {msg}" if date else f"[multiday] {msg}", flush=True)


db = MongoClient("mongo", 27017).trading_ai
coll = db[COLL]


def _day_docs(d):
    return list(coll.find({"trade_date_ist": d}).sort("timestamp", 1))


def _build_raw(docs):
    """FIXED 2026-07-15: previously keyed option series by ATM-relative label
    ("ATMp1" etc), computed fresh per-bar from that bar's OWN chain_aggregates.
    atm_strike. Since atm_strike legitimately drifts intraday (spot crossing a
    step boundary), the SAME label could mean different absolute strikes at
    different times of day, all appended into one list under that label —
    then DhanReplayIngestionServer resolved the label back to a strike using
    only ONE day-level anchor (last bar seen). Confirmed via direct Dhan API
    verification: a BankNifty 2026-06-01 trade recorded strike=54400 but its
    entry_premium (880.45) was actually strike 55000's premium — a 6-step
    (600pt) mismatch from atm drifting from 54300 (09:45) to a different
    value by end of day. Fix: key option series by ABSOLUTE STRIKE directly —
    unambiguous regardless of how atm_strike moves during the day."""
    index_bars = []
    opt_ce = {}
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
            atm_strike = atm
        for row in (p.get("strikes") or []):
            strike = row.get("strike")
            if strike is None:
                continue
            strike = int(strike)
            ts16 = str(ts)[:16]
            if row.get("ce_ltp") is not None:
                opt_ce.setdefault(strike, []).append({
                    "ts": ts16, "ce_close": row.get("ce_ltp"), "ce_iv": row.get("ce_iv"),
                    "ce_oi": row.get("ce_oi"), "ce_volume": 0,
                })
            if row.get("pe_ltp") is not None:
                opt_pe.setdefault(strike, []).append({
                    "ts": ts16, "pe_close": row.get("pe_ltp"), "pe_iv": row.get("pe_iv"),
                    "pe_oi": row.get("pe_oi"), "pe_volume": 0,
                })
    strikes_seen = set(opt_ce) | set(opt_pe)
    options = {s: {"ce": opt_ce.get(s, []), "pe": opt_pe.get(s, [])} for s in strikes_seen}
    vix_bars = [{"close": ((doc.get("payload") or {}).get("snapshot") or {}).get("vix_context", {}).get("vix_current")}
                for doc in docs]
    return {
        "index_bars": index_bars, "vix_bars": vix_bars, "options": options,
        "instrument": instrument.upper(), "step": STEP, "atm_strike": atm_strike,
        "options_keyed_by_strike": True,
    }


def _prev_day_bars_for(date):
    for offset in range(1, 6):
        pd = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
        docs = _day_docs(pd)
        if docs:
            bars = []
            for doc in docs:
                p = (doc.get("payload") or {}).get("snapshot") or {}
                fb = p.get("futures_bar") or {}
                ts = p.get("timestamp") or doc.get("timestamp") or ""
                bars.append({"ts": ts, "start_at": ts, "open": fb.get("open") or fb.get("fut_close"),
                             "high": fb.get("high"), "low": fb.get("low"),
                             "close": fb.get("fut_close"), "volume": fb.get("volume")})
            return bars
    return []



# Was raw redis.Redis(...).publish(TOPIC, ...) -- plain Pub/Sub. strategy_app's
# consumer moved to Redis Streams (XREADGROUP, confirmed by its own startup
# log: "stream consumer group created stream=..."); Pub/Sub messages are a
# completely separate mechanism and are never delivered to a Streams
# consumer. This script silently published into the void -- no error,
# emitted counts looked fine, but strategy_app_historical never saw a single
# bar (confirmed via its consumer health log: events=0 for the entire run).
# Found 2026-07-23 running the first OOS validation replay since the
# streams migration. Fixed to use the same RedisEventBus.publish() path
# snapshot_app's live producer uses, which XADDs when the resolved name
# starts with "stream:" (see contracts_app/event_bus.py).
_bus = RedisEventBus()
_stream_name = stream_name_for_topic(TOPIC)
log(f"publishing via Redis Streams: topic={TOPIC} -> stream={_stream_name}", "multiday")
interval = 60.0 / max(1.0, speed)

# One server, one builder, alive for the whole run — the entire point of
# this script. First day's raw data seeds the server at construction.
first_docs = _day_docs(dates[0])
if not first_docs:
    log(f"NO_DOCS for first requested day {dates[0]} — nothing to seed the server with", dates[0])
    sys.exit(1)
first_raw = _build_raw(first_docs)
first_prev_bars = _prev_day_bars_for(dates[0])
first_p = (first_docs[0].get("payload") or {}).get("snapshot") or {}
first_expiry = (first_p.get("session_context") or {}).get("expiry")

_current_prev_close = {"v": None}


def _velocity_ctx(trade_date):
    return (_current_prev_close["v"], None, None) if _current_prev_close["v"] is not None else (None, None, None)


srv = DhanReplayIngestionServer(first_raw, prev_day_bars=first_prev_bars, expiry_date=first_expiry)
srv.start()
builder = LiveMarketSnapshotBuilder(
    instrument=f"{instrument.upper()}FUT",
    market_api_base=srv.base_url,
    dashboard_api_base=srv.base_url,
    velocity_context_provider=_velocity_ctx,
)
log(f"server+builder started, will stay alive across {len(dates)} days", "multiday")

total_emitted = 0
try:
    for day_i, date in enumerate(dates):
        if day_i == 0:
            docs, raw, prev_bars, expiry_date = first_docs, first_raw, first_prev_bars, first_expiry
        else:
            docs = _day_docs(date)
            if not docs:
                log("NO_DOCS — day not in quality-gated collection, skipping", date)
                continue
            raw = _build_raw(docs)
            prev_bars = _prev_day_bars_for(date)
            p0 = (docs[0].get("payload") or {}).get("snapshot") or {}
            expiry_date = (p0.get("session_context") or {}).get("expiry")
            srv.set_day(raw, prev_day_bars=prev_bars, expiry_date=expiry_date)

        try:
            _current_prev_close["v"] = float(prev_bars[-1]["close"]) if prev_bars else None
        except (TypeError, ValueError):
            _current_prev_close["v"] = None

        n_opts = sum(len(v["ce"]) + len(v["pe"]) for v in raw["options"].values())
        log(f"reshaped {len(raw['index_bars'])} index bars, {n_opts} option bars, prev_day_bars={len(prev_bars)}", date)

        emitted = 0
        last_snapshot_id = None
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
                snapshot=snapshot, source="dhan_replay_multiday_from_mongo",
                metadata={"run_id": f"replay-multiday-{date}-{instrument.lower()}",
                          "replay_date": date, "bar_index": i})
            _bus.publish(_stream_name, event)
            emitted += 1
            if emitted % 100 == 0:
                log(f"published {emitted}/{len(raw['index_bars'])}", date)
            if interval > 0.005:
                time.sleep(interval)

        total_emitted += emitted
        log(f"DONE emitted={emitted} total_emitted={total_emitted}", date)
finally:
    srv.stop()

log(f"ALL DAYS DONE total_emitted={total_emitted} topic={TOPIC}", "multiday")

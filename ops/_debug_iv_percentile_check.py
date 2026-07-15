#!/usr/bin/env python3
"""One-off diagnostic: print iv_percentile per bar across several days to
verify the continuous multiday builder actually accumulates real IV history
(not pinned at 100). Not part of the permanent pipeline."""
import sys
from datetime import datetime, timedelta

instrument = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"
dates = sys.argv[2].split(",") if len(sys.argv) > 2 else []

from pymongo import MongoClient
from market_data_dashboard.services.dhan_replay_ingestion_server import DhanReplayIngestionServer
from snapshot_app.core.market_snapshot import LiveMarketSnapshotBuilder

STEP = 100 if instrument.upper() == "BANKNIFTY" else 50
COLL = "phase1_market_snapshots_hist" if instrument.upper() == "BANKNIFTY" else "phase1_market_snapshots_hist_nifty"
db = MongoClient("mongo", 27017).trading_ai
coll = db[COLL]


def _day_docs(d):
    return list(coll.find({"trade_date_ist": d}).sort("timestamp", 1))


def _build_raw(docs):
    index_bars, opt_ce, opt_pe, atm_strike = [], {}, {}, None
    for doc in docs:
        p = (doc.get("payload") or {}).get("snapshot") or {}
        fb = p.get("futures_bar") or {}
        ts = p.get("timestamp") or doc.get("timestamp") or ""
        index_bars.append({"ts": ts, "start_at": ts, "open": fb.get("open") or fb.get("fut_close"),
                            "high": fb.get("high"), "low": fb.get("low"),
                            "close": fb.get("fut_close"), "volume": fb.get("volume")})
        atm = (p.get("chain_aggregates") or {}).get("atm_strike")
        if atm:
            atm_strike = atm
        for row in (p.get("strikes") or []):
            strike = row.get("strike")
            if strike is None or atm is None:
                continue
            offset = round((strike - atm) / STEP)
            label = "ATM" if offset == 0 else (f"ATMp{offset}" if offset > 0 else f"ATMm{-offset}")
            ts16 = str(ts)[:16]
            if row.get("ce_ltp") is not None:
                opt_ce.setdefault(label, []).append({"ts": ts16, "ce_close": row.get("ce_ltp"),
                                                       "ce_iv": row.get("ce_iv"), "ce_oi": row.get("ce_oi"), "ce_volume": 0})
            if row.get("pe_ltp") is not None:
                opt_pe.setdefault(label, []).append({"ts": ts16, "pe_close": row.get("pe_ltp"),
                                                       "pe_iv": row.get("pe_iv"), "pe_oi": row.get("pe_oi"), "pe_volume": 0})
    labels = set(opt_ce) | set(opt_pe)
    options = {lbl: {"ce": opt_ce.get(lbl, []), "pe": opt_pe.get(lbl, [])} for lbl in labels}
    vix_bars = [{"close": ((doc.get("payload") or {}).get("snapshot") or {}).get("vix_context", {}).get("vix_current")} for doc in docs]
    return {"index_bars": index_bars, "vix_bars": vix_bars, "options": options,
            "instrument": instrument.upper(), "step": STEP, "atm_strike": atm_strike}


def _prev_bars(date):
    for off in range(1, 6):
        pd = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=off)).strftime("%Y-%m-%d")
        docs = _day_docs(pd)
        if docs:
            out = []
            for doc in docs:
                p = (doc.get("payload") or {}).get("snapshot") or {}
                fb = p.get("futures_bar") or {}
                ts = p.get("timestamp") or doc.get("timestamp") or ""
                out.append({"ts": ts, "start_at": ts, "open": fb.get("open") or fb.get("fut_close"),
                            "high": fb.get("high"), "low": fb.get("low"),
                            "close": fb.get("fut_close"), "volume": fb.get("volume")})
            return out
    return []


first_docs = _day_docs(dates[0])
first_raw = _build_raw(first_docs)
srv = DhanReplayIngestionServer(first_raw, prev_day_bars=_prev_bars(dates[0]))
srv.start()
builder = LiveMarketSnapshotBuilder(instrument=f"{instrument.upper()}FUT",
                                     market_api_base=srv.base_url, dashboard_api_base=srv.base_url)

for day_i, date in enumerate(dates):
    if day_i > 0:
        docs = _day_docs(date)
        raw = _build_raw(docs)
        srv.set_day(raw, prev_day_bars=_prev_bars(date))
    else:
        raw = first_raw
    n = len(raw["index_bars"])
    hist_len_start = len(builder.state.iv_history_non_expiry) + len(builder.state.iv_history_expiry)
    percentiles = []
    for i in range(n):
        srv.set_bar(i)
        try:
            snap = builder.build_snapshot(ohlc_limit=800)
        except Exception as e:
            continue
        ivp = (snap.get("iv_derived") or {}).get("iv_percentile")
        if ivp is not None:
            percentiles.append(ivp)
    hist_len_end = len(builder.state.iv_history_non_expiry) + len(builder.state.iv_history_expiry)
    pct100 = sum(1 for p in percentiles if p == 100.0)
    print(f"[{date}] bars={n} iv_hist_len {hist_len_start}->{hist_len_end} "
          f"n_percentiles={len(percentiles)} at_100={pct100} "
          f"min={min(percentiles) if percentiles else None} max={max(percentiles) if percentiles else None} "
          f"first5={percentiles[:5]} last5={percentiles[-5:]}", flush=True)

srv.stop()

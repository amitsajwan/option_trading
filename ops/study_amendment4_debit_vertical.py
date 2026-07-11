#!/usr/bin/env python3
"""Amendment 4 study (pre-registered 2026-07-11 night): fire x lever x debit vertical, BN.

On each movement fire-line crossing (score >= 0.30, prev bar below), ask the
LIVE direction lever (_detect_agreement_lever: mom15 + max_pain + OI all-agree)
for a side; ABSTAIN = no trade. Trade = debit vertical at recorded chain
prices (BUY ATM, SELL ATM+/-100), exit both legs at recorded prices t+5m
(10m/15m variants reported). Costs 7 pts/round trip (A3 convention).

Entry occupancy uses the LONGEST horizon (15 bars) so every horizon is scored
on the same entry set. Run via disposable container on the compose network:
  docker run --rm --network option_trading_default -v /tmp/a4.py:/a4.py \
    option_trading-seller_app python /a4.py
"""
import json
import math
from collections import defaultdict

import joblib
import pandas as pd
from pymongo import MongoClient

from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.ml.bundle_inference import build_feature_row
from strategy_app.brain.regime_director import _detect_agreement_lever
from snapshot_app.core.compression_features import add_compression_features
from snapshot_app.core.feature_engine import _ema

BUNDLE = "/shared_run/training_view/movement_banknifty_v1.joblib"
FIRE = 0.30
STEP = 100          # BN strike step
COST_PTS = 7.0      # 4 slippage + 3 statutory per round trip (A3 convention)
LOT = 30
HORIZONS = (5, 10, 15)
OCCUPY = max(HORIZONS)

db = MongoClient("mongo", 27017).trading_ai
coll = db.phase1_market_snapshots_hist
b = joblib.load(BUNDLE)
med = b.get("medians") or {}
feats_list = b["features"]

def leg_price(payload, strike, opt):
    for row in payload.get("strikes") or []:
        if row.get("strike") == strike:
            v = row.get(f"{opt}_ltp")
            return float(v) if v is not None else None
    return None

def vertical_value(payload, atm, side):
    """Value (pts) of BUY ATM / SELL ATM+step vertical at this bar's prices."""
    if side == "CE":
        buy, sell = leg_price(payload, atm, "ce"), leg_price(payload, atm + STEP, "ce")
    else:
        buy, sell = leg_price(payload, atm, "pe"), leg_price(payload, atm - STEP, "pe")
    if buy is None or sell is None:
        return None
    return buy - sell

days = sorted(d for d in coll.distinct("trade_date_ist") if d)
print(f"days={len(days)} {days[0]}..{days[-1]}", flush=True)

trades = []          # dict: day, hhmm, side, atm, entry_val, exits {h: val}, fut move
abstains = 0
unpriceable = 0
fires_total = 0

for di, day in enumerate(days):
    bars = []
    for s in coll.find({"trade_date_ist": day}).sort("timestamp", 1):
        p = (s.get("payload") or {}).get("snapshot") or {}
        if p.get("strikes"):
            bars.append(p)
    if len(bars) < 30:
        continue
    # Per-day EMA/compression enrichment via the CANONICAL modules — mirrors
    # build_training_view_from_mongo._enrich_ema_family. The bundle's 0.30
    # fire-line was calibrated on the training view (EMA present); scoring the
    # raw serving row leaves EMA NaN -> median -> scores never reach 0.30
    # (first run of this study: 0 fires in 412 days = that skew, not reality).
    day_rows = []
    for p in bars:
        row = build_feature_row(SnapshotAccessor(p), feats_list) or {}
        fb = p.get("futures_bar") or {}
        row["_fc"], row["_fh"], row["_fl"], row["_fv"] = (
            fb.get("fut_close"), fb.get("high"), fb.get("low"), fb.get("volume"))
        day_rows.append(row)
    df = pd.DataFrame({
        "close": [r["_fc"] for r in day_rows], "high": [r["_fh"] for r in day_rows],
        "low": [r["_fl"] for r in day_rows], "volume": [r["_fv"] for r in day_rows]})
    close = df["close"].astype(float)
    for span in (9, 21, 50):
        df[f"ema_{span}"] = _ema(close, span)
    df["day_high"] = df["high"].cummax()
    df["day_low"] = df["low"].cummin()
    add_compression_features(df)
    nz = close.replace(0.0, float("nan"))
    for span in (9, 21, 50):
        df[f"ema_{span}_slope"] = df[f"ema_{span}"].diff() / nz
    for i, r in enumerate(day_rows):
        for col in df.columns:
            if col in ("close", "high", "low", "volume"):
                continue
            cur = r.get(col)
            if cur is None or (isinstance(cur, float) and math.isnan(cur)):
                v = df[col].iloc[i]
                r[col] = None if pd.isna(v) else float(v)
    rows = []
    for row in day_rows:
        rows.append([
            med.get(f, 0.0) if (row.get(f) is None or (isinstance(row.get(f), float) and math.isnan(row.get(f)))) else row.get(f)
            for f in feats_list])
    scores = b["model"].predict_proba(rows)[:, 1]
    open_until = -1
    prev = 0.0
    for i, (p, sc) in enumerate(zip(bars, scores)):
        crossing = sc >= FIRE and prev < FIRE
        prev = sc
        if not crossing or i <= open_until or i + max(HORIZONS) >= len(bars) + 15:
            continue
        fires_total += 1
        acc = SnapshotAccessor(p)
        verdict = _detect_agreement_lever(acc)
        if verdict.side not in ("CE", "PE"):
            abstains += 1
            continue
        atm = acc.atm_strike
        entry_val = vertical_value(p, atm, verdict.side) if atm else None
        if entry_val is None or entry_val <= 0:
            unpriceable += 1
            continue
        exits = {}
        for h in HORIZONS:
            j = min(i + h, len(bars) - 1)
            exits[h] = vertical_value(bars[j], atm, verdict.side)
        fut0 = acc.fut_close
        jf = min(i + 15, len(bars) - 1)
        fut1 = SnapshotAccessor(bars[jf]).fut_close
        trades.append({
            "day": day, "i": i, "side": verdict.side, "atm": atm,
            "entry": entry_val, "exits": exits,
            "fut_move": (fut1 - fut0) if (fut0 and fut1) else None,
        })
        open_until = i + OCCUPY
    if (di + 1) % 50 == 0:
        print(f"  ...{di+1}/{len(days)} days, trades={len(trades)} fires={fires_total} abstain={abstains}", flush=True)

print(f"\nFIRES={fires_total} abstain={abstains} ({abstains/max(fires_total,1):.0%}) "
      f"unpriceable={unpriceable} trades={len(trades)}", flush=True)

mid = days[len(days) // 2]
for h in HORIZONS:
    rows = [(t, t["exits"][h]) for t in trades if t["exits"].get(h) is not None]
    pnls = [(ex - t["entry"] - COST_PTS) for t, ex in rows]
    if not pnls:
        print(f"h={h}m: no trades"); continue
    tot = sum(pnls)
    h1 = sum(p for (t, _), p in zip(rows, pnls) if t["day"] < mid)
    h2 = sum(p for (t, _), p in zip(rows, pnls) if t["day"] >= mid)
    ho = sum(p for (t, _), p in zip(rows, pnls) if t["day"] >= "2026-04")
    nho = sum(1 for (t, _), p in zip(rows, pnls) if t["day"] >= "2026-04")
    win = sum(1 for p in pnls if p > 0) / len(pnls)
    ce = [p for (t, _), p in zip(rows, pnls) if t["side"] == "CE"]
    pe = [p for (t, _), p in zip(rows, pnls) if t["side"] == "PE"]
    print(f"h={h}m: n={len(pnls)} total={tot:+.1f}pts (Rs{tot*LOT:+,.0f}) win={win:.0%} "
          f"| half1={h1:+.1f} half2={h2:+.1f} | holdout26-04+={ho:+.1f} (n={nho}) "
          f"| CE n={len(ce)} {sum(ce):+.1f} PE n={len(pe)} {sum(pe):+.1f}", flush=True)

# Direction hit-rate: did futures move in the lever's direction over 15m?
hits = [t for t in trades if t["fut_move"] is not None]
ok = sum(1 for t in hits if (t["fut_move"] > 0) == (t["side"] == "CE"))
print(f"lever 15m direction hit-rate on fires: {ok}/{len(hits)} = {ok/max(len(hits),1):.0%}", flush=True)
print(json.dumps([{k: (v if k != 'exits' else v) for k, v in t.items()} for t in trades][:8], default=str)[:1500])

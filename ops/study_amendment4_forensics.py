#!/usr/bin/env python3
"""Forensics on the Amendment 4 result (FAIL, 30 trades, all-negative).
User question: lever hit direction 57%, so why did the vertical still lose?

Re-runs the exact same fire/lever/vertical logic as study_amendment4_debit_
vertical.py but logs full per-trade diagnostics instead of just P&L:
  - entry debit (pts) and debit-as-%-of-width (richness at fire)
  - ATM-relative strike positions at entry AND at exit (chain may have moved
    enough that "ATM+100" is no longer the same distance from spot)
  - fut move over each horizon + whether it matched lever direction
  - IV at entry vs exit (extrinsic-value crush check)
  - split: direction-right-but-PNL-negative vs direction-wrong

Run identically to the A4 study (disposable container, VM Mongo).
"""
import json
import math

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
STEP = 100
COST_PTS = 7.0
LOT = 30
HORIZONS = (5, 10, 15)
OCCUPY = max(HORIZONS)

db = MongoClient("mongo", 27017).trading_ai
coll = db.phase1_market_snapshots_hist
b = joblib.load(BUNDLE)
med = b.get("medians") or {}
feats_list = b["features"]


def leg(payload, strike, opt, field="ce_ltp"):
    for row in payload.get("strikes") or []:
        if row.get("strike") == strike:
            return row
    return None


def vert_detail(payload, atm, side):
    if side == "CE":
        b_row, s_row = leg(payload, atm, "ce"), leg(payload, atm + STEP, "ce")
        b_p = b_row.get("ce_ltp") if b_row else None
        s_p = s_row.get("ce_ltp") if s_row else None
        b_iv = b_row.get("ce_iv") if b_row else None
    else:
        b_row, s_row = leg(payload, atm, "pe"), leg(payload, atm - STEP, "pe")
        b_p = b_row.get("pe_ltp") if b_row else None
        s_p = s_row.get("pe_ltp") if s_row else None
        b_iv = b_row.get("pe_iv") if b_row else None
    if b_p is None or s_p is None:
        return None
    return {"value": float(b_p) - float(s_p), "buy_leg_px": float(b_p),
            "sell_leg_px": float(s_p), "buy_leg_iv": b_iv}


days = sorted(d for d in coll.distinct("trade_date_ist") if d)
trades = []
fires_total = abstains = unpriceable = 0

for di, day in enumerate(days):
    bars = []
    for s in coll.find({"trade_date_ist": day}).sort("timestamp", 1):
        p = (s.get("payload") or {}).get("snapshot") or {}
        if p.get("strikes"):
            bars.append(p)
    if len(bars) < 30:
        continue
    row_feats = []
    for p in bars:
        row = build_feature_row(SnapshotAccessor(p), feats_list) or {}
        row_feats.append(row)
    df = pd.DataFrame({
        "close": [(p.get("futures_bar") or {}).get("fut_close") for p in bars],
        "high": [(p.get("futures_bar") or {}).get("high") for p in bars],
        "low": [(p.get("futures_bar") or {}).get("low") for p in bars],
        "volume": [(p.get("futures_bar") or {}).get("volume") for p in bars],
    })
    close = df["close"].astype(float)
    for span in (9, 21, 50):
        df[f"ema_{span}"] = _ema(close, span)
    df["day_high"] = df["high"].cummax(); df["day_low"] = df["low"].cummin()
    add_compression_features(df)
    nz = close.replace(0.0, float("nan"))
    for span in (9, 21, 50):
        df[f"ema_{span}_slope"] = df[f"ema_{span}"].diff() / nz
    for i, row in enumerate(row_feats):
        for col in df.columns:
            if col in ("close", "high", "low", "volume"):
                continue
            cur = row.get(col)
            if cur is None or (isinstance(cur, float) and math.isnan(cur)):
                v = df[col].iloc[i]
                if not pd.isna(v):
                    row[col] = float(v)
    rows = [[med.get(f, 0.0) if (r.get(f) is None or (isinstance(r.get(f), float) and math.isnan(r.get(f)))) else r.get(f)
             for f in feats_list] for r in row_feats]
    scores = b["model"].predict_proba(rows)[:, 1]

    # data-repair pass for max_pain / atm oi (same as A4 study)
    oi_hist = []
    for bi, p in enumerate(bars):
        rows_s = [r for r in (p.get("strikes") or []) if r.get("strike") and (r.get("ce_oi") or r.get("pe_oi"))]
        oi_map = {r["strike"]: (float(r.get("ce_oi") or 0), float(r.get("pe_oi") or 0)) for r in rows_s}
        oi_hist.append(oi_map)
        if len(rows_s) >= 10:
            ks = sorted(oi_map)
            def _payout(S, m=oi_map):
                return sum(c * max(0, S - K) + q * max(0, K - S) for K, (c, q) in m.items())
            ca = p.setdefault("chain_aggregates", {})
            if not ca.get("max_pain"):
                ca["max_pain"] = min(ks, key=_payout)
        atm = (p.get("chain_aggregates") or {}).get("atm_strike")
        if atm and bi >= 30 and atm in oi_map and atm in oi_hist[bi - 30]:
            ao = p.setdefault("atm_options", {})
            if ao.get("atm_ce_oi_change_30m") is None:
                ao["atm_ce_oi_change_30m"] = oi_map[atm][0] - oi_hist[bi - 30][atm][0]
                ao["atm_pe_oi_change_30m"] = oi_map[atm][1] - oi_hist[bi - 30][atm][1]

    open_until = -1
    prev = 0.0
    for i, (p, sc) in enumerate(zip(bars, scores)):
        crossing = sc >= FIRE and prev < FIRE
        prev = sc
        if not crossing or i <= open_until or i + OCCUPY >= len(bars):
            continue
        fires_total += 1
        acc = SnapshotAccessor(p)
        verdict = _detect_agreement_lever(acc)
        if verdict.side not in ("CE", "PE"):
            abstains += 1
            continue
        atm = acc.atm_strike
        ed = vert_detail(p, atm, verdict.side) if atm else None
        if ed is None or ed["value"] <= 0:
            unpriceable += 1
            continue
        fut0 = acc.fut_close
        rec = {
            "day": day, "i": i, "side": verdict.side, "atm": atm, "score": round(float(sc), 3),
            "entry_debit": round(ed["value"], 2), "entry_buy_iv": ed["buy_leg_px"] and ed.get("buy_leg_iv"),
            "fut0": fut0, "exits": {},
        }
        for h in HORIZONS:
            j = min(i + h, len(bars) - 1)
            pj = bars[j]
            xd = vert_detail(pj, atm, verdict.side)
            fut_j = SnapshotAccessor(pj).fut_close
            rec["exits"][h] = {
                "value": round(xd["value"], 2) if xd else None,
                "buy_leg_iv": xd.get("buy_leg_iv") if xd else None,
                "fut": fut_j,
                "fut_move": round(fut_j - fut0, 1) if (fut_j and fut0) else None,
                "dir_correct": ((fut_j - fut0 > 0) == (verdict.side == "CE")) if (fut_j and fut0) else None,
            }
        trades.append(rec)
        open_until = i + OCCUPY

print(f"FIRES={fires_total} abstain={abstains} unpriceable={unpriceable} trades={len(trades)}\n")

for h in HORIZONS:
    rows = [(t, t["exits"][h]) for t in trades if t["exits"].get(h, {}).get("value") is not None]
    print(f"=== horizon {h}m ===")
    dir_right = [(t, e) for t, e in rows if e["dir_correct"]]
    dir_wrong = [(t, e) for t, e in rows if e["dir_correct"] is False]
    def _stats(label, grp):
        if not grp:
            print(f"  {label}: n=0"); return
        pnl = [e["value"] - t["entry_debit"] - COST_PTS for t, e in grp]
        precost = [e["value"] - t["entry_debit"] for t, e in grp]
        win = sum(1 for x in pnl if x > 0) / len(pnl)
        print(f"  {label}: n={len(grp)} post-cost total={sum(pnl):+.1f} avg={sum(pnl)/len(pnl):+.2f} "
              f"pre-cost avg={sum(precost)/len(precost):+.2f} win={win:.0%}")
    _stats("direction RIGHT", dir_right)
    _stats("direction WRONG", dir_wrong)
    avg_debit = sum(t["entry_debit"] for t, _ in rows) / len(rows) if rows else 0
    avg_move = sum(abs(e["fut_move"]) for t, e in rows if e["fut_move"] is not None) / max(1, len(rows))
    print(f"  avg entry debit={avg_debit:.1f}pts (width={STEP}pts, debit/width={avg_debit/STEP:.0%}) "
          f"avg |fut move|={avg_move:.1f}pts cost/debit={COST_PTS/max(avg_debit,0.01):.0%}")

print("\n=== full trade dump ===")
for t in trades:
    print(json.dumps(t, default=str))

#!/usr/bin/env python3
"""Economics test for the rare-label entry model (pre-registered 2026-07-12,
docs/ENTRY_MODEL_RARELABEL_REBUILD_2026-07-12.md, "Amendment 2 / economics
test"). Same design as Amendment 4 (movement model): on each fire (score
>=0.30, prev bar <0.30, no position open), ask the live direction lever for
a side (ABSTAIN = no trade), buy a debit vertical at recorded chain prices,
exit at t+5/10/15m at the same strikes' recorded prices. Costs = 7pts/round
trip (A3/A4 convention, held constant for comparability).
"""
import math

import joblib
import pandas as pd
from pymongo import MongoClient

from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.ml.bundle_inference import build_feature_row
from strategy_app.brain.regime_director import _detect_agreement_lever
from snapshot_app.core.compression_features import add_compression_features
from snapshot_app.core.feature_engine import _ema, _adx

BUNDLE = "/shared_run/training_view/entry_rarelabel_banknifty_v2.joblib"
FIRE = 0.30
STEP = 100
COST_PTS = 7.0
LOT = 30
HORIZONS = (5, 10, 15)
OCCUPY = max(HORIZONS)

db = MongoClient("mongo", 27017).trading_ai
coll = db.phase1_market_snapshots_hist
b = joblib.load(BUNDLE)
med = b.get("feature_medians") or b.get("medians") or {}
feats_list = b["features"]
print(f"model: {b['source']} label={b['label_definition']}", flush=True)
print(f"features: {len(feats_list)}, holdout AUC={b['holdout_eval']['roc_auc']}", flush=True)


def leg_row(payload, strike):
    for r in payload.get("strikes") or []:
        if r.get("strike") == strike:
            return r
    return None


def vert_value(payload, atm, side):
    if side == "CE":
        br, sr = leg_row(payload, atm), leg_row(payload, atm + STEP)
        bp = br.get("ce_ltp") if br else None
        sp = sr.get("ce_ltp") if sr else None
    else:
        br, sr = leg_row(payload, atm), leg_row(payload, atm - STEP)
        bp = br.get("pe_ltp") if br else None
        sp = sr.get("pe_ltp") if sr else None
    if bp is None or sp is None:
        return None
    return float(bp) - float(sp)


days = sorted(d for d in coll.distinct("trade_date_ist") if d)
print(f"days={len(days)} {days[0]}..{days[-1]}", flush=True)

trades = []
abstains = unpriceable = fires_total = 0

for di, day in enumerate(days):
    bars = []
    for s in coll.find({"trade_date_ist": day}).sort("timestamp", 1):
        p = (s.get("payload") or {}).get("snapshot") or {}
        if p.get("strikes"):
            bars.append(p)
    if len(bars) < 30:
        continue

    row_feats = [build_feature_row(SnapshotAccessor(p), feats_list) or {} for p in bars]
    df = pd.DataFrame({
        "close": [(p.get("futures_bar") or {}).get("fut_close") for p in bars],
        "high": [(p.get("futures_bar") or {}).get("high") for p in bars],
        "low": [(p.get("futures_bar") or {}).get("low") for p in bars],
        "volume": [(p.get("futures_bar") or {}).get("volume") for p in bars],
    })
    close = df["close"].astype(float)
    high = df["high"].astype(float); low = df["low"].astype(float)
    for span in (9, 21, 50):
        df[f"ema_{span}"] = _ema(close, span)
    df["day_high"] = df["high"].cummax(); df["day_low"] = df["low"].cummin()
    add_compression_features(df)   # gives ema_spread_9_21/21_50, ema_order, range_*, etc
    nz = close.replace(0.0, float("nan"))
    for span in (9, 21, 50):
        df[f"ema_{span}_slope"] = df[f"ema_{span}"].diff() / nz
    try:
        df["adx_14"] = _adx(high, low, close, 14)
    except Exception:
        df["adx_14"] = float("nan")
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

    # data repair: max_pain / atm oi change (same as A4)
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
        entry_val = vert_value(p, atm, verdict.side) if atm else None
        if entry_val is None or entry_val <= 0:
            unpriceable += 1
            continue
        exits = {}
        for h in HORIZONS:
            j = min(i + h, len(bars) - 1)
            exits[h] = vert_value(bars[j], atm, verdict.side)
        trades.append({"day": day, "i": i, "side": verdict.side, "atm": atm,
                        "entry": entry_val, "exits": exits})
        open_until = i + OCCUPY
    if (di + 1) % 50 == 0:
        print(f"  ...{di+1}/{len(days)} days, trades={len(trades)} fires={fires_total} abstain={abstains}", flush=True)

print(f"\nFIRES={fires_total} abstain={abstains} ({abstains/max(fires_total,1):.0%}) "
      f"unpriceable={unpriceable} trades={len(trades)}\n", flush=True)

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

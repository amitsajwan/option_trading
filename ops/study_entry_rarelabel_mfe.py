#!/usr/bin/env python3
"""MFE (max favorable excursion) table for the rare-label entry model
(user request 2026-07-12): for each fire, track the vertical's value on
EVERY bar (not just the 5/10/15m checkpoints) from entry to entry+15, for
BOTH the straight-lever side and the faded (opposite) side simultaneously.
Reports: fire-line threshold, model probability at fire, and max points
gained (best possible exit within the window) per duration checkpoint.
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
CHECKPOINTS = (5, 10, 15)
MAXHOLD = 15

db = MongoClient("mongo", 27017).trading_ai
coll = db.phase1_market_snapshots_hist
b = joblib.load(BUNDLE)
med = b.get("feature_medians") or b.get("medians") or {}
feats_list = b["features"]


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
print(f"days={len(days)} fire_threshold={FIRE}", flush=True)

trades = []  # per fire: {score, entry, straight side/mfe-by-cp, faded side/mfe-by-cp}
fires_total = abstains = unpriceable = 0

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
    add_compression_features(df)
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
        if not crossing or i <= open_until or i + MAXHOLD >= len(bars):
            continue
        fires_total += 1
        acc = SnapshotAccessor(p)
        verdict = _detect_agreement_lever(acc)
        if verdict.side not in ("CE", "PE"):
            abstains += 1
            continue
        atm = acc.atm_strike
        straight_side = verdict.side
        faded_side = "PE" if verdict.side == "CE" else "CE"
        entry_straight = vert_value(p, atm, straight_side) if atm else None
        entry_faded = vert_value(p, atm, faded_side) if atm else None
        if entry_straight is None or entry_faded is None or entry_straight <= 0 or entry_faded <= 0:
            unpriceable += 1
            continue
        fut0 = acc.fut_close
        sign = 1.0 if straight_side == "CE" else -1.0   # favorable direction for the straight bet
        rec = {"day": day, "score": round(float(sc), 4), "straight_side": straight_side, "atm": atm,
               "entry_straight": entry_straight, "entry_faded": entry_faded,
               "mfe_straight": {}, "mfe_faded": {}, "signed_move": {}, "final_signed_move": None,
               "final_vert_straight": None}
        run_max_s = entry_straight
        run_max_f = entry_faded
        run_max_favmove = -1e9
        for k in range(1, MAXHOLD + 1):
            vs = vert_value(bars[i + k], atm, straight_side)
            vf = vert_value(bars[i + k], atm, faded_side)
            fut_k = SnapshotAccessor(bars[i + k]).fut_close
            if vs is not None:
                run_max_s = max(run_max_s, vs)
            if vf is not None:
                run_max_f = max(run_max_f, vf)
            favmove = sign * (fut_k - fut0) if (fut_k is not None and fut0) else None
            if favmove is not None:
                run_max_favmove = max(run_max_favmove, favmove)
            if k == MAXHOLD:
                rec["final_signed_move"] = favmove
                rec["final_vert_straight"] = vs
            if k in CHECKPOINTS:
                rec["mfe_straight"][k] = run_max_s - entry_straight
                rec["mfe_faded"][k] = run_max_f - entry_faded
                rec["signed_move"][k] = run_max_favmove   # best move IN OUR FAVOR (can be negative)
        trades.append(rec)
        open_until = i + MAXHOLD
    if (di + 1) % 100 == 0:
        print(f"  ...{di+1}/{len(days)} days, trades={len(trades)}", flush=True)

print(f"\nFIRES={fires_total} abstain={abstains} unpriceable={unpriceable} trades={len(trades)}\n", flush=True)

scores = [t["score"] for t in trades]
print(f"model probability at fire — min={min(scores):.3f} avg={sum(scores)/len(scores):.3f} "
      f"max={max(scores):.3f}  (fire threshold={FIRE})\n", flush=True)

print(f"{'duration':>10} {'vert_pts(straight,avg)':>24} {'vert_pts(straight,best)':>25} "
      f"{'SIGNED_move(avg,fav+/-adv)':>27} {'pct_trades_move_AGAINST':>25}")
for k in CHECKPOINTS:
    s_vals = [t["mfe_straight"][k] for t in trades if k in t["mfe_straight"]]
    sm_vals = [t["signed_move"][k] for t in trades if k in t["signed_move"]]
    against = sum(1 for v in sm_vals if v < 0) / len(sm_vals)
    print(f"{k:>9}m {sum(s_vals)/len(s_vals):>24.2f} {max(s_vals):>25.2f} "
          f"{sum(sm_vals)/len(sm_vals):>27.2f} {against:>24.0%}")

entry_avg = sum(t["entry_straight"] for t in trades) / len(trades)
print(f"\navg entry debit = {entry_avg:.2f}pts (spread width = {STEP}pts)")

# At the FINAL bar (15m), how does the vertical's actual value compare to the
# theoretical max (STEP) when the underlying really did move deep past the
# short strike in our favor? This checks whether strike selection is sane.
deep_itm = [t for t in trades if t["final_signed_move"] is not None and t["final_signed_move"] >= STEP]
print(f"\ntrades where underlying moved >= {STEP}pts (spread width) IN OUR FAVOR by 15m: {len(deep_itm)}/{len(trades)}")
if deep_itm:
    for t in deep_itm[:10]:
        print(f"  day={t['day']} side={t['straight_side']} atm={t['atm']} entry={t['entry_straight']:.1f} "
              f"final_vert={t['final_vert_straight']:.1f} signed_move={t['final_signed_move']:.1f}")

win_final = sum(1 for t in trades if t["final_vert_straight"] is not None
               and t["final_vert_straight"] > t["entry_straight"]) / len(trades)
print(f"\n(sanity) fraction of trades where 15m final vertical value > entry: {win_final:.0%}")

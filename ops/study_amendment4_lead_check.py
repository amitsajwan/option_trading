#!/usr/bin/env python3
"""Diagnostic (user question 2026-07-12): can the movement model detect the
fire condition EARLIER, before the option chain has repriced?

For each fire bar i, walk back over the preceding 15 bars and record: model
score, the vertical's debit (BUY ATM / SELL ATM+100), and IV on the buy leg.
If richness (debit, IV) is already climbing well before i, the chain is
pricing the move ahead of / concurrently with the model's own confidence
build-up — meaning lowering the fire threshold buys a cheaper entry only if
the model's pre-fire scores also carry usable direction signal (checked too).
"""
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
LOOKBACK = 15

db = MongoClient("mongo", 27017).trading_ai
coll = db.phase1_market_snapshots_hist
b = joblib.load(BUNDLE)
med = b.get("medians") or {}
feats_list = b["features"]


def leg_row(payload, strike):
    for r in payload.get("strikes") or []:
        if r.get("strike") == strike:
            return r
    return None


def vert(payload, atm, side):
    if side == "CE":
        br, sr = leg_row(payload, atm), leg_row(payload, atm + STEP)
        bp = br.get("ce_ltp") if br else None
        sp = sr.get("ce_ltp") if sr else None
        iv = br.get("ce_iv") if br else None
    else:
        br, sr = leg_row(payload, atm), leg_row(payload, atm - STEP)
        bp = br.get("pe_ltp") if br else None
        sp = sr.get("pe_ltp") if sr else None
        iv = br.get("pe_iv") if br else None
    if bp is None or sp is None:
        return None
    return float(bp) - float(sp), iv


days = sorted(d for d in coll.distinct("trade_date_ist") if d)
lead_rows = []   # per lookback-offset: list of (score, debit, iv)
n_fires_used = 0

for day in days:
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

    prev = 0.0
    for i in range(len(bars)):
        crossing = scores[i] >= FIRE and prev < FIRE
        prev = scores[i]
        if not crossing or i < LOOKBACK:
            continue
        acc = SnapshotAccessor(bars[i])
        verdict = _detect_agreement_lever(acc)
        if verdict.side not in ("CE", "PE"):
            continue
        atm = acc.atm_strike
        if not atm:
            continue
        series = []
        for off in range(-LOOKBACK, 1):
            j = i + off
            r = vert(bars[j], atm, verdict.side)
            series.append((off, float(scores[j]), r[0] if r else None, r[1] if r else None))
        if any(x[2] is None for x in series):
            continue
        lead_rows.append(series)
        n_fires_used += 1

print(f"usable fires with full {LOOKBACK}-bar lookback: {n_fires_used}\n")
print(f"{'offset(min)':>11} {'avg_score':>10} {'avg_debit':>10} {'avg_iv':>8} {'debit_delta_vs_t0':>18}")
t0_debit_by_trade = [s[-1][2] for s in lead_rows]  # debit at fire (offset 0)
for off_idx in range(LOOKBACK + 1):
    off = off_idx - LOOKBACK
    scores_o = [s[off_idx][1] for s in lead_rows]
    debits_o = [s[off_idx][2] for s in lead_rows]
    ivs_o = [s[off_idx][3] for s in lead_rows if s[off_idx][3] is not None]
    avg_score = sum(scores_o) / len(scores_o)
    avg_debit = sum(debits_o) / len(debits_o)
    avg_iv = sum(ivs_o) / len(ivs_o) if ivs_o else float("nan")
    delta_vs_fire = sum(d - t0 for d, t0 in zip(debits_o, t0_debit_by_trade)) / len(debits_o)
    print(f"{off:>11} {avg_score:>10.3f} {avg_debit:>10.2f} {avg_iv:>8.2f} {delta_vs_fire:>18.2f}")

#!/usr/bin/env python3
"""Per-trade OOS entry quality: ML entry_prob vs BankNifty 5m move (100pt oracle).

Joins POSITION_OPEN from a replay run with ML_ENTRY votes and historical snapshots.
Classifies each entry by forward 5-bar futures excursion (matches entry_bn_5m_100pts_v1).

Usage (on VM with Mongo):
  cd /opt/option_trading && .venv/bin/python ops/gcp/analyze_oos_entry_moves.py \\
    --run-id e8ba040a-a8dd-47d1-9bf8-ceffba85e809
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, MongoClient

HORIZON_BARS = 5
MIN_POINTS = 100.0


def _to_ms(ts: Any) -> int:
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return int(ts if ts > 1e12 else ts * 1000)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.timestamp() * 1000)
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                return int(datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
            except ValueError:
                pass
        try:
            return int(float(ts) * 1000)
        except ValueError:
            pass
    return 0


def make_db() -> Any:
    host = os.getenv("MONGO_HOST", "localhost")
    port = os.getenv("MONGO_PORT", "27017")
    db_name = os.getenv("MONGO_DB") or os.getenv("MONGODB_DB") or "trading_ai"
    return MongoClient(f"mongodb://{host}:{port}", serverSelectionTimeoutMS=8000)[db_name]


def load_candles(db: Any, trade_date: str) -> List[Dict[str, float]]:
    coll = os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
    proj = {
        "timestamp": 1,
        "snapshot_id": 1,
        "payload.snapshot.futures_bar.fut_high": 1,
        "payload.snapshot.futures_bar.fut_low": 1,
        "payload.snapshot.futures_bar.fut_close": 1,
    }
    out: List[Dict[str, float]] = []
    for doc in db[coll].find({"trade_date_ist": trade_date}, proj).sort("timestamp", ASCENDING):
        fb = (doc.get("payload") or {}).get("snapshot", {}).get("futures_bar", {})
        c = fb.get("fut_close")
        if c is None:
            continue
        h = float(fb.get("fut_high") or c)
        l = float(fb.get("fut_low") or c)
        out.append(
            {
                "t": _to_ms(doc.get("timestamp")),
                "sid": str(doc.get("snapshot_id") or ""),
                "h": h,
                "l": l,
                "c": float(c),
            }
        )
    return out


def find_bar_index(candles: List[Dict[str, float]], entry_ms: int, snapshot_id: str) -> int:
    if snapshot_id:
        for i, c in enumerate(candles):
            if c.get("sid") == snapshot_id:
                return i
    best_i = 0
    best_dt = 10**18
    for i, c in enumerate(candles):
        dt = abs(c["t"] - entry_ms)
        if dt < best_dt:
            best_dt = dt
            best_i = i
    return best_i


def forward_excursion(
    candles: List[Dict[str, float]],
    idx: int,
    *,
    horizon: int = HORIZON_BARS,
) -> Optional[Dict[str, float]]:
    if idx < 0 or idx >= len(candles):
        return None
    px = candles[idx]["c"]
    if px <= 0:
        return None
    end = min(len(candles), idx + horizon + 1)
    if end <= idx + 1:
        return None
    fwd_high = max(candles[j]["h"] for j in range(idx + 1, end))
    fwd_low = min(candles[j]["l"] for j in range(idx + 1, end))
    up_pts = fwd_high - px
    down_pts = px - fwd_low
    return {
        "entry_px": px,
        "up_pts": up_pts,
        "down_pts": down_pts,
        "max_any_pts": max(up_pts, down_pts),
    }


def classify_move(direction: str, exc: Dict[str, float], *, min_points: float = MIN_POINTS) -> str:
    """Bucket for operator view."""
    d = direction.upper()
    up_pts = exc["up_pts"]
    down_pts = exc["down_pts"]
    oracle_hit = exc["max_any_pts"] >= min_points

    if d == "CE":
        fav_pts = up_pts
        adv_pts = down_pts
    elif d == "PE":
        fav_pts = down_pts
        adv_pts = up_pts
    else:
        fav_pts = exc["max_any_pts"]
        adv_pts = 0.0

    if fav_pts >= min_points:
        trade_bucket = "fav_ge_100"
    elif fav_pts >= 30:
        trade_bucket = "fav_30_99"
    elif fav_pts > 5:
        trade_bucket = "fav_5_29"
    elif adv_pts >= min_points:
        trade_bucket = "adverse_ge_100"
    elif adv_pts >= 30:
        trade_bucket = "adverse_30_99"
    else:
        trade_bucket = "flat_chop"

    return trade_bucket + ("|oracle_hit" if oracle_hit and trade_bucket != "fav_ge_100" else "")


def load_ml_entry_probs(db: Any, run_id: str) -> Dict[str, float]:
    coll = os.getenv("MONGO_COLL_STRATEGY_VOTES_HISTORICAL", "strategy_votes_historical")
    by_sid: Dict[str, float] = {}
    q = {"run_id": run_id, "strategy": "ML_ENTRY", "signal_type": "ENTRY"}
    for doc in db[coll].find(q, {"snapshot_id": 1, "confidence": 1, "raw_signals": 1}):
        sid = str(doc.get("snapshot_id") or "").strip()
        if not sid:
            continue
        raw = doc.get("raw_signals") if isinstance(doc.get("raw_signals"), dict) else {}
        prob = raw.get("entry_prob")
        if prob is None:
            prob = doc.get("confidence")
        try:
            by_sid[sid] = float(prob)
        except (TypeError, ValueError):
            continue
    return by_sid


def load_trades(db: Any, run_id: str) -> List[Dict[str, Any]]:
    coll = os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL", "strategy_positions_historical")
    pos_map: Dict[str, Dict[str, Any]] = {}
    for doc in db[coll].find({"run_id": run_id}).sort("timestamp", ASCENDING):
        pid = str(doc.get("position_id") or doc.get("payload", {}).get("position_id") or "").strip()
        if not pid:
            continue
        ev = str(doc.get("event") or "").upper()
        slot = pos_map.setdefault(pid, {})
        if ev == "POSITION_OPEN":
            slot["open"] = doc
        elif ev == "POSITION_CLOSE":
            slot["close"] = doc

    trades: List[Dict[str, Any]] = []
    for pid, slot in pos_map.items():
        if "open" not in slot or "close" not in slot:
            continue
        odoc = slot["open"]
        cdoc = slot["close"]
        op = (odoc.get("payload") or {}).get("position") or odoc
        cl = (cdoc.get("payload") or {}).get("position") or cdoc

        direction = str(odoc.get("direction") or op.get("direction") or "").upper()
        trade_date = str(
            odoc.get("trade_date_ist")
            or op.get("trade_date_ist")
            or odoc.get("trade_date")
            or op.get("trade_date")
            or ""
        ).strip()
        entry_ms = _to_ms(odoc.get("timestamp") or op.get("timestamp"))
        close_ms = _to_ms(cdoc.get("timestamp") or cl.get("timestamp"))
        entry_sid = str(
            odoc.get("entry_snapshot_id") or odoc.get("snapshot_id") or op.get("entry_snapshot_id") or ""
        ).strip()

        entry_prem = float(op.get("entry_premium") or odoc.get("entry_premium") or 0)
        exit_prem = float(cl.get("exit_premium") or cdoc.get("exit_premium") or 0)
        pnl_pct = ((exit_prem - entry_prem) / entry_prem * 100.0) if entry_prem else 0.0

        trades.append(
            {
                "position_id": pid,
                "trade_date": trade_date,
                "entry_ms": entry_ms,
                "close_ms": close_ms,
                "direction": direction,
                "entry_strategy": str(
                    odoc.get("entry_strategy") or op.get("entry_strategy") or odoc.get("entry_strategy_name") or ""
                ),
                "entry_snapshot_id": entry_sid,
                "entry_futures": float(
                    odoc.get("entry_futures_price") or op.get("entry_futures_price") or 0
                ),
                "pnl_pct": round(pnl_pct, 3),
                "exit_reason": str(cl.get("exit_reason") or cdoc.get("exit_reason") or ""),
            }
        )
    trades.sort(key=lambda t: t["entry_ms"])
    return trades


def main() -> int:
    parser = argparse.ArgumentParser(description="OOS entry move vs ML entry_prob")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json-out", default="")
    parser.add_argument("--min-points", type=float, default=MIN_POINTS)
    args = parser.parse_args()
    min_points = float(args.min_points)

    db = make_db()
    ml_by_sid = load_ml_entry_probs(db, args.run_id)
    trades = load_trades(db, args.run_id)
    if not trades:
        print(f"No closed trades for run_id={args.run_id}")
        return 1

    candle_cache: Dict[str, List[Dict[str, float]]] = {}
    rows: List[Dict[str, Any]] = []
    bucket_ctr: Counter[str] = Counter()
    oracle_ctr: Counter[str] = Counter()

    for t in trades:
        td = t["trade_date"]
        if td not in candle_cache:
            candle_cache[td] = load_candles(db, td)
        candles = candle_cache[td]
        idx = find_bar_index(candles, t["entry_ms"], t["entry_snapshot_id"])
        exc = forward_excursion(candles, idx)
        entry_prob = ml_by_sid.get(t["entry_snapshot_id"])
        if entry_prob is None and candles:
            # nearest ML vote bar: same index confidence unavailable — skip
            entry_prob = None

        adv_pts = None
        bn_bias = None
        dir_match = None
        if exc is None:
            bucket = "no_forward_data"
            up_pts = down_pts = max_any = None
            oracle_hit = None
            fav_pts = None
        else:
            up_pts = round(exc["up_pts"], 1)
            down_pts = round(exc["down_pts"], 1)
            max_any = round(exc["max_any_pts"], 1)
            oracle_hit = max_any >= min_points
            d = t["direction"]
            fav_pts = round(up_pts if d == "CE" else down_pts if d == "PE" else max_any, 1)
            adv_pts = round(down_pts if d == "CE" else up_pts if d == "PE" else 0.0, 1)
            if up_pts > down_pts + 2:
                bn_bias = "CE"
            elif down_pts > up_pts + 2:
                bn_bias = "PE"
            else:
                bn_bias = "FLAT"
            if bn_bias == "FLAT":
                dir_match = "TIE"
            elif d == bn_bias:
                dir_match = "Y"
            else:
                dir_match = "N"
            bucket = classify_move(d, exc, min_points=min_points)
            bucket_ctr[bucket.split("|")[0]] += 1
            oracle_ctr["oracle_hit_100_any_dir" if oracle_hit else "oracle_miss"] += 1

        rows.append(
            {
                **t,
                "entry_prob": round(entry_prob, 4) if entry_prob is not None else None,
                "bar_idx": idx,
                "up_pts_5m": up_pts,
                "down_pts_5m": down_pts,
                "adv_pts_5m": adv_pts,
                "fav_pts_5m": fav_pts,
                "max_any_pts_5m": max_any,
                "oracle_hit_100": oracle_hit,
                "bn_5m_bias": bn_bias,
                "det_dir_match": dir_match,
                "move_bucket": bucket,
            }
        )

    # Summary
    probs = [r["entry_prob"] for r in rows if r["entry_prob"] is not None]
    print(f"run_id={args.run_id} trades={len(rows)} ml_prob_joined={len(probs)}")
    if probs:
        sp = sorted(probs)
        print(
            f"entry_prob: min={sp[0]:.3f} p50={sp[len(sp)//2]:.3f} max={sp[-1]:.3f} avg={sum(probs)/len(probs):.3f}"
        )
    print("\n5m move buckets (trade direction):")
    for k, v in bucket_ctr.most_common():
        print(f"  {k}: {v}")
    print("\nOracle label (>=100pt either direction within 5 bars):")
    for k, v in oracle_ctr.most_common():
        print(f"  {k}: {v}")

    # PnL by bucket
    pnl_by_bucket: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        b = str(r.get("move_bucket") or "unknown").split("|")[0]
        pnl_by_bucket[b].append(float(r.get("pnl_pct") or 0))
    print("\nAvg option PnL% by 5m move bucket:")
    for k in sorted(pnl_by_bucket.keys()):
        vals = pnl_by_bucket[k]
        print(f"  {k}: n={len(vals)} avg_pnl%={sum(vals)/len(vals):.2f}")

    # Deterministic direction vs BN 5m bias
    dir_rows = [r for r in rows if r.get("det_dir_match") in ("Y", "N", "TIE")]
    if dir_rows:
        y = sum(1 for r in dir_rows if r["det_dir_match"] == "Y")
        n = sum(1 for r in dir_rows if r["det_dir_match"] == "N")
        tie = sum(1 for r in dir_rows if r["det_dir_match"] == "TIE")
        print(f"\nDeterministic CE/PE vs BN 5m dominant move (up>down+2 => CE, down>up+2 => PE):")
        print(f"  correct: {y}/{len(dir_rows)} = {100*y/len(dir_rows):.1f}%")
        print(f"  wrong:   {n}/{len(dir_rows)} = {100*n/len(dir_rows):.1f}%")
        print(f"  flat/tie: {tie}/{len(dir_rows)} = {100*tie/len(dir_rows):.1f}%")
        ce_rows = [r for r in dir_rows if r["direction"] == "CE"]
        pe_rows = [r for r in dir_rows if r["direction"] == "PE"]
        if ce_rows:
            ce_ok = sum(1 for r in ce_rows if r["det_dir_match"] == "Y")
            print(f"  CE trades (call): {ce_ok}/{len(ce_rows)} correct = {100*ce_ok/len(ce_rows):.1f}%")
        if pe_rows:
            pe_ok = sum(1 for r in pe_rows if r["det_dir_match"] == "Y")
            print(f"  PE trades (put):  {pe_ok}/{len(pe_rows)} correct = {100*pe_ok/len(pe_rows):.1f}%")
        det_only = [r for r in dir_rows if r.get("entry_strategy") == "DET_DIRECTION"]
        if det_only:
            d_ok = sum(1 for r in det_only if r["det_dir_match"] == "Y")
            d_n = sum(1 for r in det_only if r["det_dir_match"] == "N")
            print(
                f"  DET_DIRECTION only: correct {d_ok}/{len(det_only)} = {100*d_ok/len(det_only):.1f}%"
                f"  wrong {d_n}"
            )

    # Cross-tab prob decile vs oracle hit
    with_prob = [r for r in rows if r["entry_prob"] is not None and r["oracle_hit_100"] is not None]
    if with_prob:
        hits = sum(1 for r in with_prob if r["oracle_hit_100"])
        print(f"\nOracle hit rate (all with prob): {hits}/{len(with_prob)} = {100*hits/len(with_prob):.1f}%")
        high = [r for r in with_prob if r["entry_prob"] >= 0.60]
        low = [r for r in with_prob if r["entry_prob"] < 0.55]
        if high:
            h_hits = sum(1 for r in high if r["oracle_hit_100"])
            print(f"  prob>=0.60: oracle_hit {h_hits}/{len(high)} = {100*h_hits/len(high):.1f}%")
        if low:
            l_hits = sum(1 for r in low if r["oracle_hit_100"])
            print(f"  prob<0.55: oracle_hit {l_hits}/{len(low)} = {100*l_hits/len(low):.1f}%")

    print("\nFull per-trade list (# = row, side = CE call / PE put, bn_bias = what BN did in 5m):")
    print(
        f"{'#':>3} {'date':<12} {'side':<4} {'prob':>5} {'up':>5} {'dn':>5} "
        f"{'bn':>4} {'ok':>3} {'orc':>3} {'bucket':<16} {'strategy':<20} {'pnl%':>7}"
    )
    for i, r in enumerate(rows, 1):
        prob_s = f"{r['entry_prob']:.3f}" if r["entry_prob"] is not None else "  n/a"
        up_s = f"{r['up_pts_5m']:.0f}" if r["up_pts_5m"] is not None else "n/a"
        dn_s = f"{r['down_pts_5m']:.0f}" if r["down_pts_5m"] is not None else "n/a"
        orc = "Y" if r.get("oracle_hit_100") else ("N" if r.get("oracle_hit_100") is False else "?")
        side = "CALL" if r["direction"] == "CE" else ("PUT" if r["direction"] == "PE" else r["direction"])
        print(
            f"{i:>3} {r['trade_date']:<12} {side:<4} {prob_s:>5} {up_s:>5} {dn_s:>5} "
            f"{str(r.get('bn_5m_bias') or '?'):>4} {str(r.get('det_dir_match') or '?'):>3} {orc:>3} "
            f"{str(r['move_bucket']):<16} {str(r['entry_strategy'])[:20]:<20} {r['pnl_pct']:>7.2f}"
        )

    if args.json_out:
        path = args.json_out
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"run_id": args.run_id, "rows": rows, "buckets": dict(bucket_ctr)}, fh, indent=2)
        print(f"\nWrote {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

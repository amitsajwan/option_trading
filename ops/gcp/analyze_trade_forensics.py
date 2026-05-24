#!/usr/bin/env python3
"""Per-trade forensics: entry, direction, gates, exit, missed opportunities.

Joins closed positions with ML_ENTRY votes, decision traces, and forward BN move
at entry (5-bar excursion). Use on VM after a completed historical replay.

Usage:
  python ops/gcp/analyze_trade_forensics.py \\
    --run-id ae5a86b7-9198-4e64-9399-fd5fea03e293 \\
    --date-from 2024-05-01 --date-to 2024-07-31

  python ops/gcp/analyze_trade_forensics.py --run-id <id> --csv /tmp/trades.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any, Dict, List, Optional, Tuple

try:
    from pymongo import ASCENDING, MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

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
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return int(datetime.strptime(ts.replace("+05:30", "+0530"), fmt).timestamp() * 1000)
            except ValueError:
                pass
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    return 0


def _gate_label(doc: dict) -> str:
    gate = doc.get("primary_blocker_gate")
    if isinstance(gate, dict):
        return str(gate.get("gate_id") or gate.get("reason_code") or "unknown")
    return str(gate or "unknown")


def _vote_raw(vote: dict) -> dict:
    raw = vote.get("raw_signals")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            p = json.loads(raw)
            if isinstance(p, dict):
                return p
        except json.JSONDecodeError:
            pass
    payload = vote.get("payload") or {}
    inner = payload.get("vote") if isinstance(payload, dict) else {}
    if isinstance(inner, dict):
        nested = inner.get("raw_signals")
        if isinstance(nested, dict):
            return nested
    return {}


def _pf(pnls: List[float]) -> float:
    w = sum(p for p in pnls if p > 0)
    l = abs(sum(p for p in pnls if p <= 0))
    return w / l if l > 0 else float("inf")


def make_db() -> Any:
    url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB", "trading_ai")
    return MongoClient(url, serverSelectionTimeoutMS=10000)[db_name]


def load_candles(db: Any, trade_date: str) -> List[Dict[str, float]]:
    coll = os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
    out: List[Dict[str, float]] = []
    for doc in db[coll].find({"trade_date_ist": trade_date}, {"timestamp": 1, "snapshot_id": 1, "payload": 1}).sort(
        "timestamp", ASCENDING
    ):
        fb = (doc.get("payload") or {}).get("snapshot", {}).get("futures_bar", {})
        c = fb.get("fut_close")
        if c is None:
            continue
        out.append(
            {
                "t": _to_ms(doc.get("timestamp")),
                "sid": str(doc.get("snapshot_id") or ""),
                "h": float(fb.get("fut_high") or c),
                "l": float(fb.get("fut_low") or c),
                "c": float(c),
            }
        )
    return out


def find_bar_index(candles: List[Dict[str, float]], entry_ms: int, snapshot_id: str) -> int:
    if snapshot_id:
        for i, c in enumerate(candles):
            if c.get("sid") == snapshot_id:
                return i
    best_i, best_dt = 0, 10**18
    for i, c in enumerate(candles):
        dt = abs(c["t"] - entry_ms)
        if dt < best_dt:
            best_dt, best_i = dt, i
    return best_i


def forward_excursion(candles: List[Dict[str, float]], idx: int) -> Optional[Dict[str, float]]:
    if idx < 0 or idx >= len(candles):
        return None
    px = candles[idx]["c"]
    if px <= 0:
        return None
    end = min(len(candles), idx + HORIZON_BARS + 1)
    if end <= idx + 1:
        return None
    fwd_high = max(candles[j]["h"] for j in range(idx + 1, end))
    fwd_low = min(candles[j]["l"] for j in range(idx + 1, end))
    up_pts = fwd_high - px
    down_pts = px - fwd_low
    return {"up_pts": up_pts, "down_pts": down_pts, "max_any_pts": max(up_pts, down_pts)}


def bn_bias(exc: Dict[str, float]) -> str:
    if exc["up_pts"] > exc["down_pts"] + 2:
        return "CE"
    if exc["down_pts"] > exc["up_pts"] + 2:
        return "PE"
    return "FLAT"


def _position_payload(doc: dict) -> dict:
    payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
    pos = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    return pos or {}


def load_trades(db: Any, run_id: str, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    coll = db.strategy_positions_historical
    pos_map: Dict[str, Dict[str, Any]] = {}
    for doc in coll.find({"run_id": run_id}).sort("timestamp", ASCENDING):
        pid = str(doc.get("position_id") or _position_payload(doc).get("position_id") or "").strip()
        if not pid:
            continue
        ev = str(doc.get("event") or _position_payload(doc).get("event") or "").upper()
        slot = pos_map.setdefault(pid, {})
        if ev == "POSITION_OPEN":
            slot["open"] = doc
        elif ev == "POSITION_CLOSE":
            slot["close"] = doc

    trades: List[Dict[str, Any]] = []
    for pid, slot in pos_map.items():
        if "open" not in slot or "close" not in slot:
            continue
        o, c = slot["open"], slot["close"]
        op, cl = _position_payload(o), _position_payload(c)
        td = str(
            c.get("trade_date_ist") or cl.get("trade_date_ist") or o.get("trade_date_ist") or op.get("trade_date_ist") or ""
        )[:10]
        if date_from and td < date_from:
            continue
        if date_to and td > date_to:
            continue
        entry_sid = str(
            op.get("entry_snapshot_id") or op.get("snapshot_id") or o.get("entry_snapshot_id") or o.get("snapshot_id") or ""
        ).strip()
        trades.append(
            {
                "position_id": pid,
                "trade_date": td,
                "entry_ms": _to_ms(o.get("timestamp") or op.get("timestamp")),
                "close_ms": _to_ms(c.get("timestamp") or cl.get("timestamp")),
                "entry_snapshot_id": entry_sid,
                "direction": str(cl.get("direction") or op.get("direction") or c.get("direction") or o.get("direction") or ""),
                "entry_strategy": str(
                    op.get("entry_strategy") or o.get("entry_strategy") or c.get("entry_strategy") or ""
                ),
                "regime": str(cl.get("regime") or op.get("regime") or ""),
                "exit_reason": str(cl.get("exit_reason") or c.get("exit_reason") or ""),
                "pnl_pct": float(cl.get("pnl_pct") or c.get("pnl_pct") or 0),
                "mfe_pct": float(cl.get("mfe_pct") or c.get("mfe_pct") or 0),
                "mae_pct": float(cl.get("mae_pct") or c.get("mae_pct") or 0),
                "bars_held": int(cl.get("bars_held") or c.get("bars_held") or 0),
                "ml_entry_prob": cl.get("ml_entry_prob") or c.get("ml_entry_prob"),
            }
        )
    trades.sort(key=lambda t: t["entry_ms"])
    return trades


def load_ml_votes(db: Any, run_id: str) -> Dict[str, Dict[str, Any]]:
    by_sid: Dict[str, Dict[str, Any]] = {}
    for v in db.strategy_votes_historical.find(
        {"run_id": run_id, "strategy": "ML_ENTRY", "signal_type": "ENTRY"},
        {"snapshot_id": 1, "confidence": 1, "raw_signals": 1, "direction": 1, "timestamp": 1},
    ):
        sid = str(v.get("snapshot_id") or "").strip()
        if not sid:
            continue
        raw = _vote_raw(v)
        by_sid[sid] = {
            "entry_prob": float(raw.get("entry_prob") or v.get("confidence") or 0),
            "direction_source": str(
                raw.get("direction_source") or raw.get("direction_src") or "unknown"
            ),
            "vote_direction": str(v.get("direction") or ""),
            "vote_ts": _to_ms(v.get("timestamp")),
        }
    return by_sid


def load_blockers_by_snapshot(db: Any, run_id: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for doc in db.strategy_decision_traces_historical.find(
        {"run_id": run_id, "final_outcome": "blocked"},
        {"snapshot_id": 1, "primary_blocker_gate": 1},
    ):
        sid = str(doc.get("snapshot_id") or "").strip()
        if sid and sid not in out:
            out[sid] = _gate_label(doc)
    return out


def print_funnel(
    *,
    run_id: str,
    trades: List[Dict],
    ml_votes: Dict[str, Dict],
    blockers: Counter,
    date_from: str,
    date_to: str,
) -> None:
    ml_ge65 = sum(1 for v in ml_votes.values() if v["entry_prob"] >= 0.65)
    traded_sids = {t["entry_snapshot_id"] for t in trades if t["entry_snapshot_id"]}
    ml_ge65_traded = sum(1 for sid, v in ml_votes.items() if v["entry_prob"] >= 0.65 and sid in traded_sids)

    print("\n" + "=" * 78)
    print(f"  TRADE FORENSICS — {date_from} → {date_to}")
    print(f"  run_id: {run_id}")
    print("=" * 78)
    print("\n  ── Funnel ──")
    print(f"    ML_ENTRY votes (all):     {len(ml_votes)}")
    print(f"    ML_ENTRY prob >= 0.65:    {ml_ge65}")
    print(f"    Closed trades:            {len(trades)}")
    print(f"    Votes>=0.65 → trade:      {ml_ge65_traded} ({100*ml_ge65_traded/ml_ge65:.1f}%)" if ml_ge65 else "    (no votes>=0.65)")
    if ml_ge65:
        print(f"    Missed votes (>=0.65):    {ml_ge65 - ml_ge65_traded}")

    print("\n  ── Top blockers (decision traces, blocked) ──")
    for gate, cnt in blockers.most_common(12):
        print(f"    {gate:<36} {cnt}")

    by_exit = Counter(t["exit_reason"] for t in trades)
    print("\n  ── Exit reasons ──")
    for reason, n in by_exit.most_common():
        grp = [t["pnl_pct"] for t in trades if t["exit_reason"] == reason]
        print(f"    {reason:<22} n={n:3d}  avg_pnl={mean(grp)*100:+.2f}%  PF={_pf(grp):.2f}")


def diagnose_trade(t: Dict, ml: Optional[Dict], exc: Optional[Dict]) -> List[str]:
    flags: List[str] = []
    pnl = t["pnl_pct"] * 100
    mfe = t["mfe_pct"] * 100
    mae = t["mae_pct"] * 100

    if ml:
        if ml["entry_prob"] < 0.65:
            flags.append("LOW_ENTRY_PROB")
        if ml.get("direction_source") == "direction_ml":
            pass
        elif ml.get("direction_source") == "momentum":
            flags.append("DIR_MOMENTUM_FALLBACK")
    else:
        flags.append("NO_ML_VOTE_MATCH")

    if exc:
        bias = bn_bias(exc)
        d = t["direction"].upper()
        if bias == "FLAT":
            flags.append("BN_FLAT_AT_ENTRY")
        elif d != bias:
            flags.append("DIR_WRONG_VS_BN_5M")
        else:
            flags.append("DIR_OK_VS_BN_5M")
        opp = "PE" if d == "CE" else "CE"
        opp_fav = exc["down_pts"] if opp == "PE" else exc["up_pts"]
        our_fav = exc["up_pts"] if d == "CE" else exc["down_pts"]
        if opp_fav > our_fav + 30:
            flags.append("BETTER_OPP_SIDE")

    if mfe >= 15 and pnl < mfe * 0.5:
        flags.append("LEFT_PROFIT_ON_TABLE")
    if pnl < -10 and mfe < 5:
        flags.append("BAD_ENTRY_NO_RUN")
    if t["exit_reason"] == "TIME_STOP" and pnl < -3:
        flags.append("TIME_STOP_LOSER")
    if t["exit_reason"] == "TIME_STOP" and pnl > 0 and mfe > pnl + 5:
        flags.append("TIME_STOP_EARLY")
    if t["exit_reason"] == "TRAILING_STOP" and pnl > 0:
        flags.append("TRAIL_OK")
    if t["exit_reason"] == "STOP_LOSS":
        flags.append("STOP_HIT")

    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-trade entry/direction/gate/exit forensics")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--date-from", default="2024-05-01")
    parser.add_argument("--date-to", default="2024-07-31")
    parser.add_argument("--csv", default="", help="Write per-trade CSV")
    parser.add_argument("--top", type=int, default=15, help="Show N worst/best trades")
    parser.add_argument("--min-points", type=float, default=MIN_POINTS)
    args = parser.parse_args()

    db = make_db()
    trades = load_trades(db, args.run_id, args.date_from, args.date_to)
    ml_votes = load_ml_votes(db, args.run_id)
    snap_blockers = load_blockers_by_snapshot(db, args.run_id)

    blocker_ctr: Counter = Counter()
    for doc in db.strategy_decision_traces_historical.find(
        {"run_id": args.run_id, "final_outcome": "blocked"},
        {"primary_blocker_gate": 1},
    ):
        blocker_ctr[_gate_label(doc)] += 1

    print_funnel(
        run_id=args.run_id,
        trades=trades,
        ml_votes=ml_votes,
        blockers=blocker_ctr,
        date_from=args.date_from,
        date_to=args.date_to,
    )

    if not trades:
        print("\n  No trades in window — check run_id / dates / Mongo hygiene.")
        return 1

    traded_sids = {t["entry_snapshot_id"] for t in trades}
    missed = [
        (sid, v)
        for sid, v in ml_votes.items()
        if v["entry_prob"] >= 0.65 and sid not in traded_sids
    ]
    missed_by_blocker: Counter = Counter()
    for sid, _ in missed:
        missed_by_blocker[snap_blockers.get(sid, "unknown_or_no_trace")] += 1

    print("\n  ── Missed entries (ML vote >=0.65, no trade at snapshot) ──")
    print(f"    count: {len(missed)}")
    for gate, cnt in missed_by_blocker.most_common(10):
        print(f"      {gate:<32} {cnt}")

    by_src: Dict[str, List[float]] = defaultdict(list)
    candle_cache: Dict[str, List] = {}
    rows: List[Dict[str, Any]] = []
    dir_match_ctr: Counter = Counter()
    flag_ctr: Counter = Counter()

    for t in trades:
        ml = ml_votes.get(t["entry_snapshot_id"])
        src = (ml or {}).get("direction_source", "unknown")
        by_src[src].append(t["pnl_pct"])

        td = t["trade_date"]
        if td not in candle_cache:
            candle_cache[td] = load_candles(db, td)
        candles = candle_cache[td]
        idx = find_bar_index(candles, t["entry_ms"], t["entry_snapshot_id"])
        exc = forward_excursion(candles, idx)
        flags = diagnose_trade(t, ml, exc)
        for fl in flags:
            flag_ctr[fl] += 1
            if fl.startswith("DIR_") or fl == "BETTER_OPP_SIDE":
                dir_match_ctr[fl] += 1

        capture = (t["pnl_pct"] / t["mfe_pct"]) if t["mfe_pct"] > 0.001 else None
        rows.append(
            {
                **t,
                "entry_prob": (ml or {}).get("entry_prob"),
                "direction_source": (ml or {}).get("direction_source"),
                "bn_bias_5m": bn_bias(exc) if exc else None,
                "up_pts_5m": round(exc["up_pts"], 1) if exc else None,
                "down_pts_5m": round(exc["down_pts"], 1) if exc else None,
                "flags": "|".join(flags),
                "mfe_capture": round(capture, 2) if capture is not None else None,
            }
        )

    print("\n  ── By direction_source ──")
    for src in sorted(by_src):
        pnls = by_src[src]
        print(f"    {src:<22} n={len(pnls):3d}  PF={_pf(pnls):.2f}  avg={mean(pnls)*100:+.2f}%")

    print("\n  ── Diagnosis flags (trades) ──")
    for fl, cnt in flag_ctr.most_common(20):
        print(f"    {fl:<28} {cnt}")

    print("\n  ── Direction vs BN 5m ──")
    for fl, cnt in dir_match_ctr.most_common():
        print(f"    {fl:<28} {cnt}")

    worst = sorted(rows, key=lambda r: r["pnl_pct"])[: args.top]
    best = sorted(rows, key=lambda r: r["pnl_pct"], reverse=True)[: args.top]

    def _print_trade_table(title: str, subset: List[Dict]) -> None:
        print(f"\n  ── {title} ──")
        print(f"  {'date':<12} {'dir':<3} {'pnl%':>7} {'mfe%':>7} {'prob':>5} {'src':<16} {'bn':<4} {'exit':<14} flags")
        for r in subset:
            print(
                f"  {r['trade_date']:<12} {r['direction']:<3} {r['pnl_pct']*100:>+6.1f}% "
                f"{r['mfe_pct']*100:>+6.1f}% {(r.get('entry_prob') or 0):>5.2f} "
                f"{str(r.get('direction_source') or '?')[:16]:<16} {str(r.get('bn_bias_5m') or '?'):<4} "
                f"{r['exit_reason'][:14]:<14} {r.get('flags','')[:40]}"
            )

    _print_trade_table(f"Worst {args.top} trades", worst)
    _print_trade_table(f"Best {args.top} trades", best)

    # Layer verdict
    print("\n  ── Layer verdict (heuristic) ──")
    wrong_dir = flag_ctr.get("DIR_WRONG_VS_BN_5M", 0) + flag_ctr.get("BETTER_OPP_SIDE", 0)
    exit_leak = flag_ctr.get("LEFT_PROFIT_ON_TABLE", 0) + flag_ctr.get("TIME_STOP_EARLY", 0)
    bad_entry = flag_ctr.get("BAD_ENTRY_NO_RUN", 0) + flag_ctr.get("LOW_ENTRY_PROB", 0)
    print(f"    Direction / side selection issues:  {wrong_dir} trades flagged")
    print(f"    Exit / profit capture issues:       {exit_leak} trades flagged")
    print(f"    Entry quality issues:               {bad_entry} trades flagged")
    print(f"    Missed ML entries (not converted):  {len(missed)} snapshots")
    if missed:
        top_miss = missed_by_blocker.most_common(1)[0][0]
        print(f"    Top miss blocker:                   {top_miss}")

    if args.csv:
        fields = [
            "trade_date", "direction", "pnl_pct", "mfe_pct", "mae_pct", "exit_reason", "bars_held",
            "entry_prob", "direction_source", "bn_bias_5m", "up_pts_5m", "down_pts_5m", "flags",
            "entry_snapshot_id", "position_id",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\n  Wrote {len(rows)} rows → {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

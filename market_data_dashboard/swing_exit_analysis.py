"""
Swing-exit simulation for a specific replay session.
Usage:
    python swing_exit_analysis.py --date 2024-09-25 --run b86ef70e
"""
import os, sys, argparse
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict, Any
from pymongo import MongoClient, ASCENDING


def _to_ms(ts) -> int:
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return int(ts * 1000)
    if isinstance(ts, datetime):
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


# ── Config ──────────────────────────────────────────────────────────────────

def make_db():
    host = os.getenv("MONGO_HOST", "localhost")
    port = os.getenv("MONGO_PORT", "27017")
    db   = os.getenv("MONGO_DB") or os.getenv("MONGODB_DB") or "trading_ai"
    return MongoClient(f"mongodb://{host}:{port}", serverSelectionTimeoutMS=5000)[db]


COLL_SNAPSHOTS = os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
COLL_POSITIONS = os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL", "strategy_positions_historical")


# ── Candle loading ───────────────────────────────────────────────────────────

def load_candles(db, trade_date: str) -> List[Dict]:
    proj = {
        "timestamp": 1,
        "payload.snapshot.futures_bar.fut_open":   1,
        "payload.snapshot.futures_bar.fut_high":   1,
        "payload.snapshot.futures_bar.fut_low":    1,
        "payload.snapshot.futures_bar.fut_close":  1,
    }
    candles = []
    for doc in db[COLL_SNAPSHOTS].find({"trade_date_ist": trade_date}, proj).sort("timestamp", ASCENDING):
        fb = (doc.get("payload") or {}).get("snapshot", {}).get("futures_bar", {})
        ts = doc.get("timestamp")
        if not ts:
            continue
        o = fb.get("fut_open") or fb.get("fut_close")
        h = fb.get("fut_high") or o
        l = fb.get("fut_low")  or o
        c = fb.get("fut_close") or o
        if not c:
            continue
        candles.append({"t": _to_ms(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return candles


# ── Trade loading ────────────────────────────────────────────────────────────

def load_trades(db, trade_date: str, run_prefix: str) -> List[Dict]:
    proj = {
        "timestamp": 1, "event": 1, "position_id": 1, "signal_id": 1,
        "payload": 1,
    }
    pos_map: Dict[str, Dict] = {}
    for doc in db[COLL_POSITIONS].find({"trade_date_ist": trade_date}, proj).sort("timestamp", ASCENDING):
        pid  = str(doc.get("position_id") or "").strip()
        if not pid:
            continue
        pl   = doc.get("payload") or {}
        evnt = str(doc.get("event") or pl.get("event") or "").upper()
        slot = pos_map.setdefault(pid, {"position_id": pid})
        if evnt == "POSITION_OPEN":
            slot["open"] = pl; slot["open_ts"] = doc.get("timestamp")
            slot["run_id"] = str(pl.get("run_id") or doc.get("run_id") or "")
        elif evnt == "POSITION_CLOSE":
            slot["close"] = pl; slot["close_ts"] = doc.get("timestamp")

    trades = []
    for pid, slot in pos_map.items():
        if "open" not in slot or "close" not in slot:
            continue
        run_id = slot.get("run_id", "")
        if run_prefix and not run_id.startswith(run_prefix):
            continue
        op = slot["open"]; cl = slot["close"]
        entry  = float(op.get("entry_price") or op.get("price") or 0)
        exit_p = float(cl.get("exit_price")  or cl.get("price") or 0)
        dir_   = str(op.get("direction") or op.get("dir") or "").upper()
        strat  = str(op.get("strategy") or op.get("strategy_name") or "")
        pnl_pct = ((exit_p - entry) / entry * 100) if entry else 0
        if dir_ == "SHORT":
            pnl_pct = -pnl_pct
        ots = slot["open_ts"]
        cts = slot["close_ts"]
        open_ms  = _to_ms(ots)
        close_ms = _to_ms(cts)
        trades.append({
            "position_id": pid,
            "run_id": run_id,
            "dir":   dir_,
            "strat": strat,
            "entry": entry,
            "exit":  exit_p,
            "pnl_pct": round(pnl_pct, 3),
            "open_ms":  open_ms,
            "close_ms": close_ms,
            "recipe_margin": float(op.get("recipe_margin") or op.get("target_margin") or 0),
            "stop_margin":   float(op.get("stop_margin") or op.get("sl_margin") or 0),
        })
    trades.sort(key=lambda t: t["open_ms"])
    return trades


# ── Swing detection ──────────────────────────────────────────────────────────

def swing_low(candles: List[Dict], up_to_idx: int, lookback: int = 10) -> Optional[float]:
    """Lowest low in the last `lookback` bars before entry."""
    start = max(0, up_to_idx - lookback)
    lows = [c["l"] for c in candles[start:up_to_idx + 1]]
    return min(lows) if lows else None


def swing_high(candles: List[Dict], up_to_idx: int, lookback: int = 10) -> Optional[float]:
    """Highest high in the last `lookback` bars before entry."""
    start = max(0, up_to_idx - lookback)
    highs = [c["h"] for c in candles[start:up_to_idx + 1]]
    return max(highs) if highs else None


def next_resistance(candles: List[Dict], from_idx: int, direction: str, entry: float, lookahead: int = 30) -> Optional[float]:
    """Nearest swing high/low ahead of entry as a rough target."""
    end = min(len(candles), from_idx + lookahead)
    if direction == "LONG":
        highs = [c["h"] for c in candles[from_idx:end] if c["h"] > entry]
        return min(highs) if highs else None
    else:
        lows = [c["l"] for c in candles[from_idx:end] if c["l"] < entry]
        return max(lows) if lows else None


# ── Swing exit simulation ────────────────────────────────────────────────────

def simulate_swing_exit(
    candles: List[Dict],
    entry_idx: int,
    close_idx: int,
    direction: str,
    entry_price: float,
    lookback: int = 10,
) -> Dict:
    """
    Simulate what would have happened with swing stop + first target.
    Returns dict with stop, target, actual_exit, pnl_pct, exit_reason.
    """
    if direction == "LONG":
        stop  = swing_low(candles,  entry_idx, lookback)
        tgt   = next_resistance(candles, entry_idx + 1, "LONG", entry_price)
    else:
        stop  = swing_high(candles, entry_idx, lookback)
        tgt   = next_resistance(candles, entry_idx + 1, "SHORT", entry_price)

    if stop is None:
        stop = entry_price * (0.995 if direction == "LONG" else 1.005)
    if tgt is None:
        tgt = entry_price * (1.005 if direction == "LONG" else 0.995)

    exit_price   = entry_price
    exit_reason  = "session_end"

    for c in candles[entry_idx + 1: close_idx + 2]:  # simulate bar-by-bar
        if direction == "LONG":
            if c["l"] <= stop:
                exit_price  = stop
                exit_reason = "stop_hit"
                break
            if c["h"] >= tgt:
                exit_price  = tgt
                exit_reason = "target_hit"
                break
        else:
            if c["h"] >= stop:
                exit_price  = stop
                exit_reason = "stop_hit"
                break
            if c["l"] <= tgt:
                exit_price  = tgt
                exit_reason = "target_hit"
                break
        exit_price = c["c"]  # carry forward close

    pnl = (exit_price - entry_price) / entry_price * 100
    if direction == "SHORT":
        pnl = -pnl

    return {
        "swing_stop":   round(stop, 2),
        "swing_target": round(tgt, 2),
        "swing_exit":   round(exit_price, 2),
        "swing_pnl":    round(pnl, 3),
        "exit_reason":  exit_reason,
    }


# ── Candle index lookup ──────────────────────────────────────────────────────

def nearest_idx(candles: List[Dict], ms: int) -> int:
    best, best_d = 0, abs(candles[0]["t"] - ms)
    for i, c in enumerate(candles):
        d = abs(c["t"] - ms)
        if d < best_d:
            best, best_d = i, d
    return best


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2024-09-25")
    ap.add_argument("--run",  default="b86ef70e")
    ap.add_argument("--lookback", type=int, default=10,
                    help="Bars to look back for swing high/low at entry")
    args = ap.parse_args()

    db      = make_db()
    candles = load_candles(db, args.date)
    trades  = load_trades(db, args.date, args.run)

    if not candles:
        print(f"No candles found for {args.date}")
        sys.exit(1)
    if not trades:
        print(f"No trades found for {args.date} run={args.run}")
        sys.exit(1)

    print(f"\n{'='*72}")
    print(f"  Session {args.date}  ·  run {args.run}  ·  {len(candles)} candles  ·  {len(trades)} trades")
    print(f"  Swing lookback: {args.lookback} bars")
    print(f"{'='*72}\n")

    hdr = f"{'#':>2}  {'DIR':5} {'STRAT':20} {'ENTRY':>8} {'RECIPE EXIT':>11} {'RECIPE P%':>9}  "
    hdr += f"{'SWING STOP':>10} {'SWING TGT':>9} {'SWING EXIT':>10} {'SWING P%':>8}  {'DIFF':>7}  {'REASON'}"
    print(hdr)
    print("-" * len(hdr))

    total_recipe = 0.0
    total_swing  = 0.0

    for i, tr in enumerate(trades, 1):
        ei = nearest_idx(candles, tr["open_ms"])
        ci = nearest_idx(candles, tr["close_ms"])
        sw = simulate_swing_exit(candles, ei, ci, tr["dir"], tr["entry"], args.lookback)

        diff = sw["swing_pnl"] - tr["pnl_pct"]
        total_recipe += tr["pnl_pct"]
        total_swing  += sw["swing_pnl"]

        sign = "+" if diff >= 0 else ""
        print(
            f"{i:>2}  {tr['dir']:5} {tr['strat'][:20]:20} {tr['entry']:>8.1f}"
            f"  {tr['exit']:>10.1f} {tr['pnl_pct']:>+9.3f}%"
            f"  {sw['swing_stop']:>10.1f} {sw['swing_target']:>9.1f} {sw['swing_exit']:>10.1f} {sw['swing_pnl']:>+8.3f}%"
            f"  {sign}{diff:.3f}%  {sw['exit_reason']}"
        )

    print("-" * len(hdr))
    print(f"{'TOTAL':>52} {total_recipe:>+9.3f}%{'':>34} {total_swing:>+8.3f}%  {total_swing - total_recipe:>+7.3f}%")
    print()


if __name__ == "__main__":
    main()

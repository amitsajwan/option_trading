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
        pl   = (doc.get("payload") or {}).get("position") or doc.get("payload") or {}
        evnt = str(doc.get("event") or "").upper()
        slot = pos_map.setdefault(pid, {"position_id": pid})
        if evnt == "POSITION_OPEN":
            slot["open"] = pl
            slot["open_ts"] = doc.get("timestamp")
            slot["run_id"] = str(doc.get("run_id") or pl.get("run_id") or "")
            slot["open_doc"] = doc
        elif evnt == "POSITION_CLOSE":
            slot["close"] = pl
            slot["close_ts"] = doc.get("timestamp")
            if not slot.get("run_id"):
                slot["run_id"] = str(doc.get("run_id") or pl.get("run_id") or "")

    trades = []
    for pid, slot in pos_map.items():
        if "open" not in slot or "close" not in slot:
            continue
        run_id = slot.get("run_id", "")
        if run_prefix and not run_id.startswith(run_prefix):
            continue
        op = slot["open"]; cl = slot["close"]
        odoc = slot.get("open_doc", {})

        # Entry price: futures underlying (for swing analysis), fallback to premium
        entry  = float(odoc.get("entry_futures_price") or op.get("entry_futures_price")
                       or op.get("entry_premium") or op.get("entry_price") or 0)
        exit_p = float(cl.get("exit_futures_price") or cl.get("exit_premium")
                       or cl.get("exit_price") or 0)

        # Direction: CE = bullish (like LONG), PE = bearish (like SHORT)
        raw_dir = str(odoc.get("direction") or op.get("direction") or "").upper()
        dir_    = "LONG" if raw_dir == "CE" else ("SHORT" if raw_dir == "PE" else raw_dir)

        strat = str(odoc.get("entry_strategy") or op.get("entry_strategy") or op.get("strategy") or "")
        dm    = odoc.get("decision_metrics") or {}
        recipe_margin = float(dm.get("recipe_margin") or op.get("ml_recipe_margin") or 0)

        # P&L based on premium (actual option P&L)
        entry_prem = float(op.get("entry_premium") or 0)
        exit_prem  = float(cl.get("exit_premium") or 0)
        pnl_pct = ((exit_prem - entry_prem) / entry_prem * 100) if entry_prem else 0

        ots = slot["open_ts"]; cts = slot["close_ts"]
        open_ms  = _to_ms(ots)
        close_ms = _to_ms(cts)
        trades.append({
            "position_id":    pid,
            "run_id":         run_id,
            "dir":            dir_,
            "strat":          strat,
            "entry_futures":  entry,          # futures price at entry (for swing calc)
            "entry_prem":     entry_prem,
            "exit_prem":      exit_prem,
            "pnl_pct":        round(pnl_pct, 3),
            "open_ms":        open_ms,
            "close_ms":       close_ms,
            "recipe_margin":  recipe_margin,
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
    ap.add_argument("--run",  default="",
                    help="run_id prefix filter (leave empty to get all trades for the date)")
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

    print()

    total_recipe = 0.0
    total_swing  = 0.0

    for i, tr in enumerate(trades, 1):
        ei = nearest_idx(candles, tr["open_ms"])
        ci = nearest_idx(candles, tr["close_ms"])
        # Swing is computed on futures candles; entry_futures is the reference price
        entry_fut = tr["entry_futures"] or candles[ei]["c"]
        sw = simulate_swing_exit(candles, ei, ci, tr["dir"], entry_fut, args.lookback)

        # Express swing P&L as % move in futures (apples-to-apples signal quality)
        # Also note actual option P&L separately
        diff = sw["swing_pnl"] - tr["pnl_pct"]
        total_recipe += tr["pnl_pct"]
        total_swing  += sw["swing_pnl"]

        entry_time = str(tr["open_ms"])  # we'll convert back if needed
        sign = "+" if diff >= 0 else ""
        print(
            f"{i:>2}  {tr['dir']:5} {tr['strat'][:18]:18} fut={entry_fut:>7.0f}"
            f"  prem: {tr['entry_prem']:>6.1f}→{tr['exit_prem']:>6.1f} ({tr['pnl_pct']:>+7.2f}%)"
            f"  |  swing_stop={sw['swing_stop']:>7.0f}  tgt={sw['swing_target']:>7.0f}"
            f"  fut_exit={sw['swing_exit']:>7.0f} ({sw['swing_pnl']:>+6.2f}%)"
            f"  diff={sign}{diff:.2f}%  [{sw['exit_reason']}]"
        )

    sep = "-" * 110
    print(sep)
    print(f"  TOTAL option P&L (actual):  {total_recipe:>+8.2f}%")
    print(f"  TOTAL swing futures P&L:    {total_swing:>+8.2f}%   (futures % move, not option premium)")
    print(f"  Delta:                      {total_swing - total_recipe:>+8.2f}%")
    print()
    print("  Note: swing P&L is the % move in the FUTURES price between entry and swing exit.")
    print("  Actual option P&L would be larger (leverage) but exit timing is what matters here.")
    print()


if __name__ == "__main__":
    main()
